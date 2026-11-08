# -*- coding: utf-8 -*-
"""token_estimate.py 的单元测试 —— 锁住语言感知加权估算的关键口径。"""

import token_estimate


def test_empty_returns_zero():
    assert token_estimate.estimate_tokens("") == 0


def test_pure_chinese_one_token_per_char():
    # 400 个汉字 → 400 token。老的「字符数//4」口径会低估成 100，
    # 这就是加权估算要修的核心缺陷。
    assert token_estimate.estimate_tokens("你" * 400) == 400


def test_pure_english_4_chars_per_token():
    assert token_estimate.estimate_tokens("abcd") == 1


def test_pure_english_rounds_up():
    # 5 个英文字符，5//4 向上取整 = 2
    assert token_estimate.estimate_tokens("abcde") == 2


def test_mixed_zh_en():
    # 中文 2 字（2 token）+ 英文 2 字符（向上取整 1 token）= 3
    assert token_estimate.estimate_tokens("你好ab") == 3


def test_chinese_punctuation_counts_as_cjk():
    # 中文标点（如 "。"）按 1 token/字，不按英文 4 字符
    assert token_estimate.estimate_tokens("。") == 1


def test_fullwidth_counts_as_cjk():
    # 全角字符（U+FF00 区）也按 1 token/字
    assert token_estimate.estimate_tokens("，") == 1


def test_nonempty_at_least_one():
    # 1 个英文字符也至少估 1 token
    assert token_estimate.estimate_tokens("a") == 1
