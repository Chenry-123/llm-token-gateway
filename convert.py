# -*- coding: utf-8 -*-
"""
convert.py —— 协议转换层（Anthropic ⇄ OpenAI）

网关本来是「只透传、不转协议」的。这个文件给它加一块新能力：
当「客户端说的协议」和「上游说的协议」不一样时，在中间做双向翻译。

场景（也是催生这个文件的原因）：
    Claude Code 只会说 Anthropic 话（/v1/messages），
    但用户手里的百炼 key 只会说 OpenAI 话（/v1/chat/completions）。
    要让 Claude Code 用上百炼，网关必须当这个翻译。

为什么单独一个文件、而不是塞进 server.py：
    和 protocols.py 同一个道理 —— 转换规则是「会变的数据」，
    应该和「不变的转发逻辑」分开。将来加第三种协议，只动这里。

边界（当前这一版的明确范围）：
    1. 纯文本 + 工具调用：非流式、流式都已支持（本文件三个方向都覆盖）。
    2. 图片（image block）：请求方向（anthropic→openai）已转换 —— image block
       翻成 image_url part，不再静默丢弃；能不能处理图片交给上游定（文本模型
       会自己报错，网关照实转发）。响应方向（openai 返回图片）本版仍跳过。
    3. 反向转换（openai→anthropic）未做，走错方向直接 400 说明。
    4. 转换只动「格式」，不动「语义」。系统提示、对话顺序、温度这些
       一个都不能丢、不能改，否则模型行为会变。

一个关键取舍：本文件只翻译「请求体」和「响应体」，
【不动】记账。记账仍用 protocols.py 里的 usage_from_json / StreamUsage，
从「上游原始响应」里取 —— 因为记账要的是上游的真实数字，
而不是被翻译过、可能四舍五入的数字。
"""

import json
import uuid


# ---------------------------------------------------------------
# 方向 1 · 请求：Anthropic → OpenAI
# ---------------------------------------------------------------
def _content_to_text(content):
    """把 Anthropic 的 content（可能是字符串，也可能是 blocks 数组）压成纯文本。

    纯文本阶段只认 text block。image / tool_use 等 block 直接跳过 ——
    这是本版边界的一部分，不是 bug。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
        return "".join(parts)
    return ""


def _anthropic_image_block_to_openai(block):
    """Anthropic image block → OpenAI image_url part。

    Anthropic: {"type":"image","source":{"type":"base64","media_type":"image/png","data":"..."}}
    OpenAI:    {"type":"image_url","image_url":{"url":"data:image/png;base64,..."}}
    """
    src = block.get("source") or {}
    media_type = src.get("media_type") or "image/png"
    data = src.get("data") or ""
    return {
        "type": "image_url",
        "image_url": {"url": "data:%s;base64,%s" % (media_type, data)},
    }


def _content_to_openai(content):
    """Anthropic content（字符串或 blocks 数组）→ OpenAI content。

    返回：
      - 纯文本 → 字符串（保持现状，纯文本供应商最省事）
      - 含图片 → parts 数组 [{"type":"text",...}, {"type":"image_url",...}]
    图片不再静默丢弃：转换层只负责「翻译格式」，能不能处理图片交给上游定。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    texts, images = [], []
    for block in content:
        if not isinstance(block, dict):
            continue
        t = block.get("type")
        if t == "text":
            texts.append(block.get("text") or "")
        elif t == "image":
            images.append(_anthropic_image_block_to_openai(block))
    if images:
        parts = []
        if texts:
            parts.append({"type": "text", "text": "".join(texts)})
        parts.extend(images)
        return parts
    return "".join(texts)


def _anthropic_tools_to_openai(tools):
    """Anthropic tools 定义 → OpenAI tools 定义。

    Anthropic: [{name, description, input_schema}]
    OpenAI:    [{type:"function", function:{name, description, parameters}}]
    input_schema 和 parameters 是同一份 JSON Schema，只是换个键名。
    """
    if not tools:
        return None
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        out.append({
            "type": "function",
            "function": {
                "name": t.get("name"),
                "description": t.get("description") or "",
                "parameters": t.get("input_schema")
                             or {"type": "object", "properties": {}},
            },
        })
    return out or None


def _anthropic_tool_choice_to_openai(tool_choice):
    """Anthropic tool_choice → OpenAI tool_choice。

    Anthropic: {type:"auto"|"any"|"tool"|"none", name?}
    OpenAI:    "auto" | "required" | {type:"function",function:{name}} | "none"
    """
    if not isinstance(tool_choice, dict):
        return None
    t = tool_choice.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool":
        return {"type": "function", "function": {"name": tool_choice.get("name")}}
    if t == "none":
        return "none"
    return None


def anthropic_request_to_openai(body, upstream_model):
    """把 Claude Code 发来的 Anthropic 请求，翻译成百炼要的 OpenAI 请求。

    返回一个新的 dict，绝不改动入参（上游请求还要留作记账统计用）。
    """
    if not isinstance(body, dict):
        return body

    out = {}

    # 模型名换成上游真实模型（qwen-plus 之类），这是「网关替客户端保密」的核心。
    out["model"] = upstream_model

    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]

    # 工具定义 + 工具选择。不做这一步，模型根本不知道有哪些工具可用。
    tools = _anthropic_tools_to_openai(body.get("tools"))
    if tools:
        out["tools"] = tools
    tool_choice = _anthropic_tool_choice_to_openai(body.get("tool_choice"))
    if tool_choice is not None:
        out["tool_choice"] = tool_choice

    # ！ 两家的 system 位置不一样：
    #    Anthropic 是顶层字段 system
    #    OpenAI   是 messages 数组里 role == "system" 的一条
    # 少了这一步，Claude Code 的系统提示会整个丢掉，模型就「失忆」了。
    messages = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": _content_to_text(system)})

    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        if role == "assistant":
            # assistant 可能「先说一句，再调工具」：text block + tool_use block 并存。
            text_parts = []
            tool_calls = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text_parts.append(block.get("text") or "")
                    elif block.get("type") == "tool_use":
                        tool_calls.append({
                            "id": block.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": block.get("name") or "",
                                "arguments": json.dumps(block.get("input") or {},
                                                        ensure_ascii=False),
                            },
                        })
            msg = {"role": "assistant", "content": "".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
        elif role == "user":
            # user 可能「纯文本」「图片」或「tool_result」，或三者都有。tool_result
            # 要拆成 OpenAI 的 role=="tool" 消息，一条一个结果。
            text_parts = []
            image_parts = []
            tool_results = []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text":
                        text_parts.append(block.get("text") or "")
                    elif btype == "image":
                        image_parts.append(_anthropic_image_block_to_openai(block))
                    elif btype == "tool_result":
                        tool_results.append(block)
            else:
                text_parts.append(_content_to_text(content))

            # 有图片就发 parts 数组（text 在前、image 在后），纯文本才发字符串。
            if text_parts or image_parts:
                if image_parts:
                    parts = []
                    if text_parts:
                        parts.append({"type": "text", "text": "".join(text_parts)})
                    parts.extend(image_parts)
                    messages.append({"role": "user", "content": parts})
                else:
                    messages.append({"role": "user", "content": "".join(text_parts)})

            for tr in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id") or "",
                    "content": _content_to_openai(tr.get("content")),
                })
        else:
            messages.append({"role": role, "content": _content_to_text(content)})
    out["messages"] = messages

    # 通用参数直传。这些两家语义一致，直接抄。
    for k in ("temperature", "top_p", "top_k", "stream"):
        if body.get(k) is not None:
            out[k] = body[k]

    # stop_sequences → stop，只是改个名。
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]

    return out


# ---------------------------------------------------------------
# 方向 2 · 响应：OpenAI → Anthropic
# ---------------------------------------------------------------
# finish_reason（OpenAI）→ stop_reason（Anthropic）的词典。
# 只列了纯文本阶段会碰到的几种。tool_use 等留给后续。
_STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "content_filter": "refusal",
    "tool_calls": "tool_use",
    None: "end_turn",
}


def _stop_reason(finish_reason):
    return _STOP_MAP.get(finish_reason, "end_turn")


def openai_response_to_anthropic(body, client_model):
    """把百炼返回的 OpenAI 响应，翻译成 Claude Code 要的 Anthropic 响应。

    client_model：客户端当初请求的模型名。翻译回填时要用它，
    否则 Claude Code 会发现「我点的是 A，回来的是 B」，直接报错。
    """
    if not isinstance(body, dict):
        return body

    choices = body.get("choices") or []
    choice = {}
    if choices and isinstance(choices[0], dict):
        choice = choices[0]
    message = choice.get("message") or {}
    content = message.get("content") or ""
    tool_calls = message.get("tool_calls") or []

    usage = body.get("usage") or {}

    # content 拆成 Anthropic 的 block 列表：先 text，再逐个 tool_use。
    # 一个 assistant 消息可能「说了句话 + 调了好几个工具」，都要保住。
    blocks = []
    if content:
        blocks.append({"type": "text", "text": content})
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        # arguments 是 JSON 字符串，Anthropic 的 input 要对象。解析失败退化成空对象。
        try:
            inp = json.loads(fn.get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            inp = {}
        blocks.append({
            "type": "tool_use",
            "id": tc.get("id") or "",
            "name": fn.get("name") or "",
            "input": inp,
        })

    return {
        "id": body.get("id") or "",
        "type": "message",
        "role": "assistant",
        "model": client_model,
        "content": blocks,
        # ！ finish_reason 在 choice 这一层，和 message 平级，不是 message 的子字段。
        "stop_reason": _stop_reason(choice.get("finish_reason")),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens") or 0,
            "output_tokens": usage.get("completion_tokens") or 0,
        },
    }


# ---------------------------------------------------------------
# 方向 3 · 流式响应：OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------
# 流式和非流式最大的区别：**没有「整包」可以读**。响应是几十个 chunk 排着队
# 流过来的，你要边收边翻，而且不能攒批（攒了就不是流式了）。
#
# 两家的流式格式完全不同：
#
#   OpenAI（上游百炼）           Anthropic（Claude Code 要的）
#   ------------------           ----------------------------
#   data: {delta.content}        event: message_start
#   data: {delta.content}        event: content_block_start
#   data: {finish_reason}        event: content_block_delta   ← 每段正文
#   data: {usage}                event: content_block_stop
#   data: [DONE]                 event: message_delta         ← 停因 + 输出量
#                                event: message_stop
#
# 一个明知而接受的瑕疵：Anthropic 的 message_start 里要带 input_tokens，
# 但 OpenAI 把 usage（含输入 token）放在**最后一块**。所以 message_start
# 里的输入量只能用请求侧的估算值（字符数 ÷ 4 这种），或者填 0。
# —— 权威数字不靠它：网关自己的日志仍从上游原始 usage 里取真值，
#   这个流只是给 Claude Code「看着像那么回事」用的，不影响记账。
class OpenAIStreamToAnthropic:
    """把 OpenAI 的流式 chunk 流，翻译成 Anthropic 的流式事件流。

    用法：
        tr = OpenAIStreamToAnthropic("claude-sonnet-4-5", estimated_input_tokens=40)
        for data in sse_decoder.feed(chunk):
            if data == "[DONE]":
                yield from tr.finish()
            else:
                yield from tr.feed(json.loads(data))
        yield from tr.finish()      # 上游若没发 [DONE] 直接断流，这里兜底收尾
    """

    def __init__(self, client_model, estimated_input_tokens=0):
        self.client_model = client_model
        self.message_id = "msg_" + uuid.uuid4().hex
        self.est_input = int(estimated_input_tokens or 0)
        self.input_tokens = 0
        self.output_tokens = 0
        self.stop_reason = "end_turn"
        self._started = False
        self._finished = False
        # 工具调用状态：Anthropic 的 content 是 block 列表，text 和 tool_use
        # 各自占一个 index（text 先，tool 后）。OpenAI 的 tool_calls 用自己
        # 的 index 编号（第几个工具），两者不是一回事，这里做映射。
        self._next_index = 0            # 下一个 Anthropic content_block 的 index
        self._text_index = None         # text block 占用的 index（None = 还没开）
        self._text_open = False         # text block 是否已 start 未 stop
        self._tools = {}                # openai_index -> {anthropic_index, id, name}

    def _event(self, name, payload):
        # Anthropic 的 SSE 和 OpenAI 不一样：前面多一行 event:。
        # 虽然 Anthropic 的 SDK 主要靠 data 里的 type 字段分流，
        # 但 event: 行是官方格式的一部分，照写不误 —— 兼容最保守。
        return (
            "event: %s\ndata: %s\n\n" % (name, json.dumps(payload, ensure_ascii=False))
        ).encode("utf-8")

    def _ensure_started(self, out):
        """发 message_start（整个消息的开头）。

        content_block 不再在这里发，改由 _open_text / _open_tool 按需打开，
        因为「有没有 text、有几个工具」要流到才知道。
        """
        if self._started:
            return
        self._started = True
        out.append(self._event("message_start", {
            "type": "message_start",
            "message": {
                "id": self.message_id,
                "type": "message",
                "role": "assistant",
                "model": self.client_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": self.est_input,
                    "output_tokens": 1,          # 官方格式里这里就是 1，不是 0
                },
            },
        }))

    def _open_text(self, out):
        """打开 text block。只在真有正文到来时调用。"""
        if self._text_open:
            return
        self._text_index = self._next_index
        self._next_index += 1
        self._text_open = True
        out.append(self._event("content_block_start", {
            "type": "content_block_start",
            "index": self._text_index,
            "content_block": {"type": "text", "text": ""},
        }))

    def _close_text(self, out):
        """关闭 text block（若开着）。tool_calls 到来前必须先关，否则 index 错位。"""
        if not self._text_open:
            return
        self._text_open = False
        out.append(self._event("content_block_stop", {
            "type": "content_block_stop", "index": self._text_index,
        }))

    def _open_tool(self, out, openai_index, tool_id, name):
        """打开一个新的 tool_use block，记录映射，返回它的 Anthropic index。"""
        anthropic_index = self._next_index
        self._next_index += 1
        self._tools[openai_index] = {
            "anthropic_index": anthropic_index,
            "id": tool_id or "",
            "name": name or "",
        }
        out.append(self._event("content_block_start", {
            "type": "content_block_start",
            "index": anthropic_index,
            "content_block": {"type": "tool_use", "id": tool_id or "",
                              "name": name or "", "input": {}},
        }))
        return anthropic_index

    def feed(self, payload):
        """喂一个 OpenAI chunk 的 JSON，产出 0..n 个 Anthropic 事件字节串。"""
        if self._finished or not isinstance(payload, dict):
            return []

        out = []
        self._ensure_started(out)

        for c in (payload.get("choices") or []):
            c = c or {}
            delta = c.get("delta") or {}

            # 正文增量 → content_block_delta。
            # 只认 content；Qwen 的 reasoning_content（思考过程）本版先跳过 ——
            # 真出现也不该混进正文污染语义。
            text = delta.get("content")
            if isinstance(text, str) and text:
                self._open_text(out)
                out.append(self._event("content_block_delta", {
                    "type": "content_block_delta",
                    "index": self._text_index,
                    "delta": {"type": "text_delta", "text": text},
                }))

            # 工具调用增量：OpenAI 流式的 tool_calls 是分片累积的 ——
            # 第一个 chunk 带 index/id/name，后续 chunk 只带 arguments 的碎片。
            # 这里按 index 归类，把 arguments 碎片实时转发成 input_json_delta。
            for tc in (delta.get("tool_calls") or []):
                if not isinstance(tc, dict):
                    continue
                oi = tc.get("index", 0)
                fn = tc.get("function") or {}
                if oi not in self._tools:
                    # 新工具出现：先关掉可能还开着的 text block，再开 tool_use。
                    self._close_text(out)
                    self._open_tool(out, oi, tc.get("id"), fn.get("name"))
                args = fn.get("arguments")
                if isinstance(args, str) and args:
                    anthropic_index = self._tools[oi]["anthropic_index"]
                    out.append(self._event("content_block_delta", {
                        "type": "content_block_delta",
                        "index": anthropic_index,
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    }))

            if c.get("finish_reason"):
                self.stop_reason = _stop_reason(c["finish_reason"])

        # include_usage 的末块：choices 为空，只带 usage。真值在这里捡。
        u = payload.get("usage")
        if isinstance(u, dict):
            self.input_tokens = u.get("prompt_tokens") or self.input_tokens
            self.output_tokens = u.get("completion_tokens") or self.output_tokens

        return out

    def finish(self):
        """补发收尾事件（关所有 block → message_delta → message_stop）。

        幂等：第二次调用返回空。这样才能在「[DONE] 触发」和「循环结束兜底」
        两个地方都放心调用，不会重复发。
        """
        if self._finished:
            return []
        self._finished = True

        out = []
        self._ensure_started(out)
        # 兜底：空响应也要有一个完整的空 text block（否则流的形状不完整）。
        if self._next_index == 0:
            self._open_text(out)
        # 关闭所有还开着的 block：text（若开）+ 每个 tool_use。
        self._close_text(out)
        for tool in self._tools.values():
            out.append(self._event("content_block_stop", {
                "type": "content_block_stop", "index": tool["anthropic_index"],
            }))
        out.append(self._event("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": self.output_tokens},
        }))
        out.append(self._event("message_stop", {"type": "message_stop"}))
        return out
