# -*- coding: utf-8 -*-
"""
router.py —— 模型路由（决策层的「一号决策」）

一句话：easy 的任务用便宜模型，medium/hard 用旗舰模型。
省的不是「每个任务都便宜一点」，而是「不该花旗舰钱的那部分」。

────────────────────────────────────────────────────────────────
为什么这个文件是整个项目最关键的决策，也最容易被做坏
────────────────────────────────────────────────────────────────

记住一个铁律：**路由是「有损优化」，不是「无损优化」。**

  无损优化（缓存）—— 答案一个字不变，只是少花点钱。
      → 不需要质量分，省钱就是纯赚。

  有损优化（路由）—— 换了更便宜的模型，答案可能变差。
      → 必须同时回答两个数：省了多少、质量掉了多少。
        只报「省了多少」不报「质量掉了多少」，等于骗人。

所以这个文件天生要和评测（judge.py / run_eval.py）配成一对：
路由怎么改，都要拿「成本-质量」两条线一起去量。

────────────────────────────────────────────────────────────────
难度从哪来 —— 为什么不能读人工标签
────────────────────────────────────────────────────────────────

evalset.json 里每个任务有个 difficulty 字段（easy/medium/hard）。
那是**人工标注的真值**，只用于「事后核对路由判得对不对」。

路由在运行时**绝不能读那个字段** —— 否则就是开卷考试：
它直接照着标签路由，评测再拿同一批标签打分，自己证明自己正确，
这个分数一文不值。

所以 guess_difficulty 只看请求文本本身，从文本里「猜」难度。
猜得准不准，拿人工标签当「阅卷答案」来核对 —— 这是两件事，不能混。

────────────────────────────────────────────────────────────────
第一版规则：故意写得很笨、很透明
────────────────────────────────────────────────────────────────

先用最直白、人能一眼看懂并改动的规则把链路跑通：
    - 命中「难」关键词（设计/重构/优化/架构/并发/一致性…）→ hard
    - 文本很短（≤140 字）→ easy
    - 其余 → medium

这套规则只是「占位」。它存在的意义是让评测先能跑出
「路由 vs 基线」两条对比曲线，再回头调准它。
规则越笨越好改 —— 这是刻意的，不是偷懒。
"""

# 命中任意一个 → 倾向判 hard（这些词背后通常是设计/取舍类问题）
HARD_HINTS = [
    "设计", "重构", "优化", "架构", "并发", "一致性", "方案",
    "扛", "性能", "为什么用", "怎么保证", "高并发", "缓存穿透",
]

# 文本长度阈值：最后一个用户问题 ≤ 这个长度，倾向判 easy
EASY_MAX_LEN = 140
HARD_MIN_LEN = 280


def _extract_text(block):
    """把一段 message 的 content 抽成纯文本（兼容两种协议的格式差异）。

    OpenAI 协议：content 直接是字符串。
    Anthropic 协议：content 是 list，每块是 {"type": "text", "text": ...}。
    """
    if isinstance(block, str):
        return block
    if isinstance(block, list):
        return " ".join(
            str(b.get("text") or "") for b in block
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _last_user_text(body):
    """取「最后一个用户消息」的文本。

    为什么只看最后一个 user：路由要在「这一轮」决定用哪个模型，
    而这一轮要回答的，正是用户最新问的那句话。
    历史上下文已经由缓存机制负责省钱了，路由不该重复替它操心。
    """
    msgs = body.get("messages") or []
    for m in reversed(msgs):
        if m.get("role") == "user":
            return _extract_text(m.get("content"))
    return ""


def guess_difficulty(body):
    """从请求文本猜难度，返回 "easy" / "medium" / "hard"。

    参数 body 是网关收到的完整请求体（openai 或 anthropic 协议都行）。
    不读任何难度标签 —— 只看文本。
    """
    text = _last_user_text(body)
    if not text:
        return "medium"

    n = len(text)
    if any(k in text for k in HARD_HINTS):
        return "hard"
    if n <= EASY_MAX_LEN:
        return "easy"
    if n >= HARD_MIN_LEN:
        return "hard"
    return "medium"


def pick_upstream(mc, models, body):
    """决定这次请求真正用哪个上游模型。

    返回 (upstream_model, billed_model, routed, difficulty)。

      upstream_model  真正发给上游的模型名
      billed_model    成本按谁的单价算（对外名）
      routed          这次有没有发生「换模型」
      difficulty      路由猜的难度（没开路由时是 None）

    只在 mc 配了 routing 且 enabled 时生效；否则原样透传，什么都不改。
    """
    routing = mc.get("routing") or {}
    if not routing.get("enabled"):
        return (mc.get("upstream_model"), None, False, None)

    difficulty = guess_difficulty(body)
    cheap_name = routing.get("cheap_model")
    cheap_mc = models.get(cheap_name)

    # 守卫：只允许「同厂商内降档」。跨厂商（换 base_url 或换 key）是更高级的
    # 功能，现在明确不做 —— 因为一旦 base_url/key 不同，路由就得连带换认证头
    # 和上游协议，复杂度跳级，而且现在测不了。这里宁可不动，也不能拿错 key
    # 去请求别家，报一个指向完全错误方向的 401。
    same_vendor = (
        cheap_mc
        and cheap_mc.get("base_url") == mc.get("base_url")
        and cheap_mc.get("api_key_env") == mc.get("api_key_env")
        and cheap_mc.get("protocol") == mc.get("protocol")
    )

    if difficulty == "easy" and same_vendor:
        upstream = cheap_mc.get("upstream_model") or cheap_name
        return (upstream, cheap_name, True, difficulty)

    return (mc.get("upstream_model"), None, False, difficulty)
