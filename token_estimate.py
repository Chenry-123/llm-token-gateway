# -*- coding: utf-8 -*-
"""
token_estimate.py —— 本地加权估算 token（零依赖）

为什么不用「字符数 // 4」：
    那是英文口径：4 个英文字符 ≈ 1 token。但一个中文汉字在 qwen 下就 ≈ 1 token。
    纯中文文本用 //4 会低估约 4 倍 —— 对「防超长上下文 / 防超支」这种安全闸，
    低估 = 拦不住，等于失灵。

做法（语言感知加权）：
    中文（汉字 + 中文标点 + 全角字符）按 ~1 token / 字 计；
    其余（英文 / 数字 / 空格 / 半角符号 / 换行）按 ~4 字符 / 1 token 计；
    两者加权求和，向上取整。

精度定位：
    这是「粗略上限」的升级版 —— 把中文从低估 4 倍修到量级准确。
    对 qwen 的真实分词（中文标点、英文缩写、emoji、代码片段的边界）仍有误差。
    精确 token 数以转发后上游返回的 usage 为准；本模块只负责「转发前拦住明显的超限」。
    方向偏保守（宁可略高估、早一点拦），因为安全闸漏拦比误拦代价大。
"""

import re

# 中文按 1 token/字 计的字符集合：
#   \u4e00-\u9fff   CJK 统一表意文字（常用汉字）
#   \u3400-\u4dbf   扩展 A
#   \uf900-\ufaff   兼容表意文字
#   \u3000-\u303f   CJK 符号与标点（、。《》「」… 等中文标点）
#   \uff00-\uffef   全角形式（全角字母 / 数字 / 标点，中文语境下也按 1 token/字）
CJK_RE = re.compile(
    "[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff\u3000-\u303f\uff00-\uffef]"
)


def estimate_tokens(text):
    """估算一段文本的 token 数。空串返回 0，非空至少返回 1。"""
    if not text:
        return 0
    cjk = len(CJK_RE.findall(text))
    other = len(text) - cjk
    # 中文 1 token/字 + 英文向上取整(4 字符/1 token)
    est = cjk + (other + 3) // 4
    return max(1, est)
