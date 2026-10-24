# -*- coding: utf-8 -*-
"""
protocols.py —— 协议差异表（这个项目的地基之一）

网关要同时说两种"方言"：

    openai      /v1/chat/completions    Qoder、Cursor、Codex、绝大部分国产模型
    anthropic   /v1/messages            Claude Code、Claude 系客户端

这两种协议**九成是一样的** —— 都是"收下请求 → 转发 → 返回 → 记一行用量"。
不一样的只有下面这几张表里的东西。

为什么把差异做成"表"，而不是在服务里写两个 if 分支：
    将来要加第三种协议（比如 Gemini 的 generateContent）时，
    往表里加一行数据就行，不用碰任何逻辑。
    这一类"把会变的抽成数据、把不变的写成逻辑"的思路，
    和项目里 llm.py 那一层是同一个道理 —— 你在架构说明书里见过。
"""

# ---------------------------------------------------------------
# 表 1 · 路由表
#   进来的路径 → 它属于哪个协议、往上游该拼什么路径、要不要记用量
# ---------------------------------------------------------------
ROUTES = {
    "/v1/chat/completions": {
        "protocol": "openai",
        "upstream_path": "/chat/completions",
        "counts_usage": True,
    },
    "/v1/messages": {
        "protocol": "anthropic",
        "upstream_path": "/v1/messages",
        "counts_usage": True,
    },
    # Claude Code 在真正发消息之前，会先问一句"这段上下文有多少 token"。
    # 它不计费，但**必须能通过** —— 否则客户端在第一步就报错了。
    "/v1/messages/count_tokens": {
        "protocol": "anthropic",
        "upstream_path": "/v1/messages/count_tokens",
        "counts_usage": False,
    },
}


# 上游协议 → 该协议【聊天端点】在上游的路径。
# 只有「协议转换」才会用到它：客户端路由协议 ≠ 上游协议时，路径得按【上游】协议拼。
# 不转换时仍用 ROUTES 里的 upstream_path（count_tokens 这类特殊端点不能被覆盖）。
# 不能靠 ROUTES 自动反推 —— anthropic 有两个端点（messages 和 count_tokens），会互相覆盖。
PROTOCOL_CHAT_PATH = {
    "openai": "/chat/completions",
    "anthropic": "/v1/messages",
}


# ---------------------------------------------------------------
# 表 2 · 认证与必须透传的请求头
# ---------------------------------------------------------------
PROTOCOL_AUTH = {
    "openai": {
        # 转发时把这个头替换成上游的 key
        "header": "authorization",
        "format": "Bearer {key}",
        # 没有什么头是"必须"带的
        "pass_headers": [],
    },
    "anthropic": {
        "header": "x-api-key",
        "format": "{key}",
        # ！ Anthropic 官方文档**点名**要求：网关必须原样透传下面这几个头，
        #   不许改、不许剥。文档原话是"有些网关会把这些头改掉或丢掉"。
        #   少一个，Claude Code 就会报很奇怪的不兼容错误 —— 而且你会以为是自己的 bug。
        "pass_headers": ["anthropic-version", "anthropic-beta", "content-type"],
    },
}


# ---------------------------------------------------------------
# 表 3 · 非流式响应：从 JSON 里搬用量字段
#   两家的字段名不一样，这里统一成网关内部一致的四个数字：
#       input / output / cache_read / cache_write
# ---------------------------------------------------------------
def usage_from_json(protocol, body):
    """把上游返回的 JSON 里的用量，翻译成统一格式。"""
    if not isinstance(body, dict):
        return {}

    # 有些端点（比如 Anthropic 的 count_tokens）**根本没有 usage 对象**。
    # 这种情况必须返回空字典，而不是返回一串 0 ——
    # 因为"用量是 0"和"这次没有用量"是两件事。
    # 混在一起，后面的成本统计就会被一堆假 0 拉低。
    u = body.get("usage")
    if not isinstance(u, dict) or not u:
        return {}

    if protocol == "openai":
        details = u.get("prompt_tokens_details") or {}
        return {
            "input_tokens": u.get("prompt_tokens") or 0,
            "output_tokens": u.get("completion_tokens") or 0,
            # ！ 缓存命中的那部分按折扣价计费，缓存创建的部分按溢价计费。
            #   prompt_tokens 是「总输入」，已经包含了这两部分，
            #   所以成本计算时要把它们从 input 里减掉，否则双重计费。
            "cache_read_tokens": details.get("cached_tokens") or 0,
            "cache_write_tokens": details.get("cache_creation_input_tokens") or 0,
        }

    if protocol == "anthropic":
        return {
            "input_tokens": u.get("input_tokens") or 0,
            "output_tokens": u.get("output_tokens") or 0,
            "cache_read_tokens": u.get("cache_read_input_tokens") or 0,
            "cache_write_tokens": u.get("cache_creation_input_tokens") or 0,
        }

    return {}


# ---------------------------------------------------------------
# 表 4 · 流式响应：边转发边捡用量
#   两家的落点完全不同，这是这一步唯一的技术难点：
#
#     OpenAI    需要请求里带 stream_options.include_usage=true，
#               它才会在 data: [DONE] 之前多吐一个 chunk，
#               usage 在那个 chunk 里（注意：它的 choices 是空数组）。
#
#     Anthropic 不用要，它本来就给 —— 而且是分两处给的：
#               message_start 给输入，message_delta 给输出，要拼起来。
# ---------------------------------------------------------------
class StreamUsage:
    """流式用量的累加器。基类管"字符数兜底"，两个子类各管自己捡哪里。"""

    def __init__(self, protocol):
        self.protocol = protocol
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.out_chars = 0        # 模型实际吐了多少字符（拿不到用量时的兜底）
        self.saw_usage = False    # 有没有真捡到数字。没捡到 → 这条记录必须标记为"估算"
        self.stop_reason = None

    def feed(self, payload):
        raise NotImplementedError

    def as_dict(self):
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "output_chars": self.out_chars,
            # ！ 这个布尔值比 token 数字本身还重要。
            #   它标出"这个数字是上游告诉我的，还是我自己数出来的"。
            #   把估算值和实测值混在一起做统计，是你这个项目最该避免的事。
            "usage_reported": self.saw_usage,
            "stop_reason": self.stop_reason,
        }


class OpenAIStreamUsage(StreamUsage):
    def feed(self, payload):
        for c in (payload.get("choices") or []):
            c = c or {}
            delta = c.get("delta") or {}

            for key in ("content", "reasoning_content"):
                v = delta.get(key)
                if isinstance(v, str):
                    self.out_chars += len(v)

            if c.get("finish_reason"):
                self.stop_reason = c["finish_reason"]

        u = payload.get("usage")
        if isinstance(u, dict):
            details = u.get("prompt_tokens_details") or {}
            self.input_tokens = u.get("prompt_tokens") or self.input_tokens
            self.output_tokens = u.get("completion_tokens") or self.output_tokens
            self.cache_read_tokens = details.get("cached_tokens") or 0
            self.cache_write_tokens = details.get("cache_creation_input_tokens") or 0
            self.saw_usage = True


class AnthropicStreamUsage(StreamUsage):
    def feed(self, payload):
        t = payload.get("type")

        if t == "message_start":
            u = (payload.get("message") or {}).get("usage") or {}
            self.input_tokens = u.get("input_tokens") or 0
            self.cache_read_tokens = u.get("cache_read_input_tokens") or 0
            self.cache_write_tokens = u.get("cache_creation_input_tokens") or 0
            self.saw_usage = True

        elif t == "content_block_delta":
            d = payload.get("delta") or {}
            for key in ("text", "thinking"):
                v = d.get(key)
                if isinstance(v, str):
                    self.out_chars += len(v)

        elif t == "message_delta":
            u = payload.get("usage") or {}
            # ！ 注意这里是"覆盖"不是"累加" —— 它给的是累计输出量。
            #   写成 += 的话，多段输出会让数字翻倍，而且不报错。
            if isinstance(u.get("output_tokens"), int):
                self.output_tokens = u["output_tokens"]
                self.saw_usage = True
            d = payload.get("delta") or {}
            if d.get("stop_reason"):
                self.stop_reason = d["stop_reason"]


STREAM_USAGE = {
    "openai": OpenAIStreamUsage,
    "anthropic": AnthropicStreamUsage,
}


# ---------------------------------------------------------------
# 表 5 · 请求改写
#   全项目**只有这一处**会动请求体。理由和风险都写在这儿。
# ---------------------------------------------------------------
def inject_stream_usage(protocol, body, enabled=True):
    """流式请求里，为了让上游把用量吐出来，需要往请求体里加一个参数。

    返回 True 表示"我改了请求体"。

    为什么不惜动请求体也要做这件事：
        不动它，流式就没有任何用量数字；没有数字，这个项目
        就没有一样东西可以被证明 —— 我们做的全部事情都是"量化"。

    为什么必须做成可关的开关（配置里那个 inject_include_usage）：
        多出来的那个 chunk 理论上可能让某个客户端不高兴。
        真出问题时，关掉它，网关就退化成"只记字符数"。
        ——「能退」比「能省」重要。
    """
    if not enabled:
        return False
    if protocol != "openai":
        # Anthropic 本来就给用量，不用注入。
        return False
    if not isinstance(body, dict):
        return False

    opts = body.get("stream_options") or {}
    if opts.get("include_usage") is True:
        return False

    body["stream_options"] = dict(opts, include_usage=True)
    return True


def inject_explicit_cache(protocol, body, enabled=True):
    """给「稳定的前缀」打 cache_control 标记，让上游走【显式缓存】。

    返回 True 表示"我改了请求体"。

    为什么需要这个（和隐式缓存的分野）：
        隐式缓存（自动）命中率不确定，实测前缀超 1024 token 照样 0 命中；
        显式缓存（主动打标记）是**确定性命中**，且命中价 10%（比隐式 20% 更省）。
        代价是：创建缓存那次要按 125% 计费（比隐式的 100% 贵）。
        所以它是「重复前缀越多越赚」的优化 —— 正是多轮对话/长 system 的场景。

    标记打在哪（关键设计决策）：
        只打【system 消息】的最末尾一个 text 块，不动 user 消息。
        因为 system 是多轮里最稳定、最长的前缀；user 每轮都变，打了也白打。
        OpenAI 协议下 system 是 messages 里 role=="system" 的一条；
        Anthropic 协议下 system 是顶层字段，content 是分块的 list。
    """
    if not enabled:
        return False
    if not isinstance(body, dict):
        return False

    if protocol == "openai":
        for m in body.get("messages") or []:
            if isinstance(m, dict) and m.get("role") == "system":
                content = m.get("content")
                if isinstance(content, str):
                    m["content"] = [
                        {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
                    ]
                    return True
                if isinstance(content, list) and content:
                    # 只给最后一个 text 块打标记
                    for block in reversed(content):
                        if isinstance(block, dict) and block.get("type") == "text":
                            block["cache_control"] = {"type": "ephemeral"}
                            return True
        return False

    if protocol == "anthropic":
        system = body.get("system")
        if isinstance(system, str):
            body["system"] = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
            return True
        if isinstance(system, list) and system:
            for block in reversed(system):
                if isinstance(block, dict) and block.get("type") == "text":
                    block["cache_control"] = {"type": "ephemeral"}
                    return True
        return False

    return False


# ---------------------------------------------------------------
# 表 6 · 请求统计量
#   只算"形状"，不存原文 —— 这样日志可以安全公开，
#   同时"钱花在哪类任务上"这类分析还做得出来。
# ---------------------------------------------------------------
# 这些 key 下面的字符串是二进制/超长数据（图片 base64、URL），
# 数进去会把真正的信号淹掉，所以跳过。
_SKIP_KEYS = {"data", "url", "image_url", "b64_json", "source", "signature"}


def _count_chars(obj):
    if isinstance(obj, str):
        return len(obj)
    if isinstance(obj, list):
        return sum(_count_chars(x) for x in obj)
    if isinstance(obj, dict):
        return sum(_count_chars(v) for k, v in obj.items() if k not in _SKIP_KEYS)
    return 0


def _has_image(obj):
    if isinstance(obj, dict):
        if obj.get("type") in ("image", "image_url"):
            return True
        return any(_has_image(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_image(x) for x in obj)
    return False


def request_stats(protocol, body):
    """算这次请求的"统计量"。注意：一个字的内容都不会被存下来。"""
    stats = {
        "msg_count": 0,
        "prompt_chars": 0,
        "system_chars": 0,
        "tool_count": 0,
        "declared_tools": 0,
        "has_image": False,
    }
    if not isinstance(body, dict):
        return stats

    msgs = body.get("messages") or []
    stats["msg_count"] = len(msgs)
    for m in msgs:
        if isinstance(m, dict):
            # 正文
            stats["prompt_chars"] += _count_chars(m.get("content"))
            # 工具调用（函数名 + 参数）也占 token，别漏
            stats["prompt_chars"] += _count_chars(m.get("tool_calls"))

    system = body.get("system")          # anthropic 的写法
    if system is not None:
        stats["system_chars"] = _count_chars(system)

    tools = body.get("tools") or []
    stats["tool_count"] = len(tools)
    stats["declared_tools"] = len(tools)  # 语义同上，留一个更直白的名字

    stats["has_image"] = _has_image(body)
    return stats


# ---------------------------------------------------------------
# SSE 解码器
#   两种协议共用这一个 —— 它们都是 SSE，区别只是 OpenAI 只有 data: 行，
#   Anthropic 前面多一行 event:。
#   但 Anthropic 的 JSON 载荷里本来就有 "type" 字段，所以 event: 行可以直接无视。
# ---------------------------------------------------------------
class SSEDecoder:
    """把字节流切成一条条 SSE 事件的 data 载荷。

    用法：
        for data in decoder.feed(chunk):
            payload = json.loads(data)

    设计上的取舍：只处理"一行一个 data:"的标准写法。
    SSE 规范允许一个事件里有多行 data，但这两家都不这么用，
    为一个不存在的场景写代码，是给自己制造 bug。
    """

    def __init__(self):
        self._buf = ""
        self._data_lines = []

    def feed(self, chunk):
        """喂进一段 bytes，产出 0 个或多个 data 字符串。"""
        out = []
        self._buf += chunk.decode("utf-8", errors="replace")

        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip("\r")

            if line.startswith("data:"):
                self._data_lines.append(line[5:].lstrip())
            elif line == "":
                # 空行 = 一个事件结束
                if self._data_lines:
                    out.append("".join(self._data_lines))
                    self._data_lines = []
        return out
