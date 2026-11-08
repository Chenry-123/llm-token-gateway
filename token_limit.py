# -*- coding: utf-8 -*-
"""
token_limit.py —— 单次请求的 token 上限（输入预检 + 输出钳制）

和 budget.py / task_limit.py 的分工：
    budget.py    管「钱」：窗口内累计花了多少人民币，超了拦（HTTP 402）。
    task_limit.py 管「一个任务」累计用了多少 token（跨多次请求）。
    本模块管「单次请求」的 token 数：这一条消息的输入 / 输出 token 各设一个硬顶。

两个动作：
    1. 输入预检：转发前用「本次输入估算 token」判断，超了直接拒（402），
       防「一次喂一个超大上下文」。
    2. 输出钳制：给请求体设置 / 收紧 max_tokens，防「模型一次吐太多」。

估算口径：输入 token 数由 protocols.request_stats 用 token_estimate 的
    语言感知加权口径算好（中文 ~1 token/字、英文 ~4 字符/1 token），
    本模块只负责比较、不自己估算。精确输入 token 要等上游返回 usage 才知道，
    这里是「粗略上限」不是「精确计数」。
"""


def check_input(config, prompt_tokens, system_tokens):
    """判断本次输入估算 token 是否超限，超了返回 (402, message)，否则 None。"""
    tc = config.get("token_limit") or {}
    if not tc.get("enabled"):
        return None
    max_input = tc.get("max_input_tokens") or 0
    if max_input <= 0:
        return None

    est = int(prompt_tokens) + int(system_tokens)
    if est > max_input:
        return (
            402,
            "本次输入约 %d token（加权估算），超过单次上限 %d。"
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
