# -*- coding: utf-8 -*-
"""
token_limit.py —— 单次请求的 token 上限（输入预检 + 输出钳制）

和 budget.py 的分工：
    budget.py 管「钱」：窗口内累计花了多少人民币，超了拦（HTTP 402）。
    本模块管「单次请求的 token 数」：这一条消息的输入 / 输出 token 各设一个硬顶。

两个动作：
    1. 输入预检：转发前估算本次输入的 token 数，超了直接拒（402），
       防「一次喂一个超大上下文」。
    2. 输出钳制：给请求体设置 / 收紧 max_tokens，防「模型一次吐太多」。

估算口径：token ≈ 字符数 // 4，与 budget.py、server.py 流式估算同一个系数。
这是英文口径，中文会偏低 —— 所以它是「粗略上限」，不是「精确计数」；
真正精确的输入 token 数要等上游返回 usage 才知道。这是刻意的取舍：
   不引入 tiktoken 这类分词依赖，保持网关零重依赖、可离线自验。
"""


def check_input(config, prompt_chars, system_chars):
    """估算本次输入 token，超了返回 (402, message)，否则返回 None。"""
    tc = config.get("token_limit") or {}
    if not tc.get("enabled"):
        return None
    max_input = tc.get("max_input_tokens") or 0
    if max_input <= 0:
        return None

    est = (int(prompt_chars) + int(system_chars)) // 4
    if est > max_input:
        return (
            402,
            "本次输入约 %d token（按字符数估算），超过单次上限 %d。"
            "请缩短上下文后重试，或调高 config.json 里的 token_limit.max_input_tokens。"
            % (est, max_input),
        )
    return None


def clamp_output(config, body):
    """给请求体设置 / 收紧 max_tokens，限制输出 token 数。返回是否真的改了。

    只收紧、不放宽：客户端自己设了更小的 max_tokens 就不动它。
    OpenAI 兼容接口（百炼）和 Anthropic 都用 max_tokens 这个字段名，
    所以这里统一处理，不必区分协议。
    """
    tc = config.get("token_limit") or {}
    if not tc.get("enabled"):
        return False
    max_output = tc.get("max_output_tokens") or 0
    if max_output <= 0:
        return False

    current = body.get("max_tokens")
    if current is None or current > max_output:
        body["max_tokens"] = max_output
        return True
    return False
