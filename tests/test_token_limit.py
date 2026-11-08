# -*- coding: utf-8 -*-
"""token_limit.py 的单元测试 —— 锁住单次请求 token 上限的拦截/钳制边界。"""

import token_limit


# ---- check_input：输入预检（收的是「已估算好的 token 数」）----

def test_check_input_disabled_returns_none():
    cfg = {"token_limit": {"enabled": False, "max_input_tokens": 100}}
    assert token_limit.check_input(cfg, prompt_tokens=400, system_tokens=0) is None


def test_check_input_zero_cap_returns_none():
    cfg = {"token_limit": {"enabled": True, "max_input_tokens": 0}}
    assert token_limit.check_input(cfg, prompt_tokens=400, system_tokens=0) is None


def test_check_input_missing_block_returns_none():
    assert token_limit.check_input({}, prompt_tokens=400, system_tokens=0) is None


def test_check_input_under_cap_returns_none():
    # 100 token 正好等于上限 → 不拦（边界是「超了才拦」）
    cfg = {"token_limit": {"enabled": True, "max_input_tokens": 100}}
    assert token_limit.check_input(cfg, prompt_tokens=100, system_tokens=0) is None


def test_check_input_equality_is_pass():
    # prompt + system 正好等于上限也放行，只有 > 才拦
    cfg = {"token_limit": {"enabled": True, "max_input_tokens": 100}}
    assert token_limit.check_input(cfg, prompt_tokens=50, system_tokens=50) is None


def test_check_input_over_cap_returns_402():
    cfg = {"token_limit": {"enabled": True, "max_input_tokens": 100}}
    result = token_limit.check_input(cfg, prompt_tokens=250, system_tokens=0)
    assert result is not None
    assert result[0] == 402
    assert "250" in result[1] and "100" in result[1]


def test_check_input_sums_prompt_and_system():
    # 70 + 60 = 130 > 100 → 拦
    cfg = {"token_limit": {"enabled": True, "max_input_tokens": 100}}
    result = token_limit.check_input(cfg, prompt_tokens=70, system_tokens=60)
    assert result is not None
    assert result[0] == 402


# ---- clamp_output：输出钳制 ----

def test_clamp_output_disabled_returns_false():
    cfg = {"token_limit": {"enabled": False, "max_output_tokens": 100}}
    body = {"max_tokens": 1000}
    assert token_limit.clamp_output(cfg, body) is False
    assert body["max_tokens"] == 1000  # 原样不动


def test_clamp_output_zero_cap_returns_false():
    cfg = {"token_limit": {"enabled": True, "max_output_tokens": 0}}
    body = {"max_tokens": 1000}
    assert token_limit.clamp_output(cfg, body) is False
    assert body["max_tokens"] == 1000


def test_clamp_output_sets_when_missing():
    # 客户端没设 max_tokens → 补上上限值
    cfg = {"token_limit": {"enabled": True, "max_output_tokens": 256}}
    body = {}
    assert token_limit.clamp_output(cfg, body) is True
    assert body["max_tokens"] == 256


def test_clamp_output_tightens_larger():
    cfg = {"token_limit": {"enabled": True, "max_output_tokens": 256}}
    body = {"max_tokens": 8192}
    assert token_limit.clamp_output(cfg, body) is True
    assert body["max_tokens"] == 256


def test_clamp_output_keeps_smaller():
    # 客户端设了更小的值 → 只收紧不放宽，不动它
    cfg = {"token_limit": {"enabled": True, "max_output_tokens": 256}}
    body = {"max_tokens": 128}
    assert token_limit.clamp_output(cfg, body) is False
    assert body["max_tokens"] == 128


def test_clamp_output_keeps_equal():
    # 客户端设的值正好等于上限 → 无需改
    cfg = {"token_limit": {"enabled": True, "max_output_tokens": 256}}
    body = {"max_tokens": 256}
    assert token_limit.clamp_output(cfg, body) is False
    assert body["max_tokens"] == 256
