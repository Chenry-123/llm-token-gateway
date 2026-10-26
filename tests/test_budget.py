# -*- coding: utf-8 -*-
"""budget.py 的单元测试 —— 锁住预算闸门的放行/拦截边界。"""

import pytest

import budget


CFG = {
    "models": {
        "gw-qwen": {
            "price_per_million": {
                "input": 0.8,
                "output": 2.0,
                "cache_read": 0.16,
                "cache_write": 0.8,
            }
        },
    },
    "budget": {"enabled": True, "window": "total", "cap_yuan": 50.0, "preflight": False},
    "log": {"path": "logs/usage.jsonl"},
}


def test_estimate_input_cost():
    # 400 字符 // 4 = 100 token，按全价输入
    assert budget.estimate_input_cost(CFG, "gw-qwen", 400) == pytest.approx(100 * 0.8 / 1e6)


def test_estimate_input_cost_unknown_model_none():
    assert budget.estimate_input_cost(CFG, "no-model", 400) is None


def test_estimate_input_cost_at_least_one_token():
    # 0 字符也按至少 1 token 估，避免除以零或估成 0
    assert budget.estimate_input_cost(CFG, "gw-qwen", 0) == pytest.approx(0.8 / 1e6)


def test_check_disabled_returns_none():
    cfg = {"budget": {"enabled": False}}
    assert budget.check(cfg, billed_model="gw-qwen", total_input_chars=100) is None


def test_check_zero_cap_returns_none():
    cfg = {"budget": {"enabled": True, "cap_yuan": 0}}
    assert budget.check(cfg, billed_model="gw-qwen", total_input_chars=100) is None


def test_window_start_total_is_none():
    assert budget._window_start("total") is None


def test_in_window_no_start_always_true():
    assert budget._in_window({"ts": "2026-01-01T00:00:00+08:00"}, None) is True


def test_check_hard_limit_blocks(monkeypatch):
    # 已花 60 超过上限 50 → 硬上限拦截
    monkeypatch.setattr(budget, "spent", lambda config, window="total": 60.0)
    result = budget.check(CFG, billed_model="gw-qwen", total_input_chars=100)
    assert result is not None
    assert result[0] == 402


def test_check_preflight_blocks_big_input(monkeypatch):
    # 预检：已花 49.999，本次输入 400 万字符估 0.8 元，合计超 50 → 拦截
    monkeypatch.setattr(budget, "spent", lambda config, window="total": 49.999)
    cfg = {
        "models": CFG["models"],
        "budget": {"enabled": True, "cap_yuan": 50.0, "preflight": True},
    }
    result = budget.check(cfg, billed_model="gw-qwen", total_input_chars=4_000_000)
    assert result is not None
    assert result[0] == 402


def test_check_under_cap_passes(monkeypatch):
    monkeypatch.setattr(budget, "spent", lambda config, window="total": 10.0)
    result = budget.check(CFG, billed_model="gw-qwen", total_input_chars=100)
    assert result is None
