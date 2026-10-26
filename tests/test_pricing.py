# -*- coding: utf-8 -*-
"""pricing.py 的单元测试 —— 重点锁住「四段计费不重复算缓存」这条最容易错的路。"""

import pytest

import pricing


CFG = {
    "models": {
        "gw-qwen": {
            "price_per_million": {
                "input": 0.8,
                "output": 2.0,
                "cache_read": 0.16,
                "cache_write": 0.8,
                "cache_read_explicit": 0.08,
                "cache_write_explicit": 1.0,
            }
        },
        "gw-qwen-flash": {
            "price_per_million": {
                "input": 0.15,
                "output": 1.5,
                "cache_read": 0.03,
                "cache_write": 0.15,
            }
        },
    }
}


def test_plain_input_output():
    rec = {"virtual_model": "gw-qwen", "input_tokens": 1_000_000, "output_tokens": 1_000_000}
    assert pricing.cost_of(rec, CFG) == pytest.approx(0.8 + 2.0)


def test_cache_not_double_counted():
    # input_tokens 是总输入，已含缓存部分；只对 uncached 部分按全价
    rec = {
        "virtual_model": "gw-qwen",
        "input_tokens": 1000,
        "cache_read_tokens": 500,
        "cache_write_tokens": 100,
    }
    expected = 400 * 0.8 / 1e6 + 500 * 0.16 / 1e6 + 100 * 0.8 / 1e6
    assert pricing.cost_of(rec, CFG) == pytest.approx(expected)


def test_uncached_clamped_at_zero():
    # 缓存 token 超过总输入（脏数据）时，uncached 应归 0 而不是负数
    rec = {"virtual_model": "gw-qwen", "input_tokens": 100, "cache_read_tokens": 200}
    assert pricing.cost_of(rec, CFG) == pytest.approx(200 * 0.16 / 1e6)


def test_unknown_model_returns_zero():
    rec = {"virtual_model": "no-such-model", "input_tokens": 1000}
    assert pricing.cost_of(rec, CFG) == 0.0


def test_bill_by_billed_model_not_virtual():
    # 路由后钱按实际用的便宜模型算，不按客户端请求的原模型
    rec = {"virtual_model": "gw-qwen", "billed_model": "gw-qwen-flash", "input_tokens": 1_000_000}
    assert pricing.cost_of(rec, CFG) == pytest.approx(0.15)


def test_explicit_cache_uses_explicit_price():
    rec = {
        "virtual_model": "gw-qwen",
        "explicit_cache": True,
        "input_tokens": 1000,
        "cache_read_tokens": 500,
    }
    # uncached = 500；显式读价 0.08（不是隐式 0.16）
    expected = 500 * 0.8 / 1e6 + 500 * 0.08 / 1e6
    assert pricing.cost_of(rec, CFG) == pytest.approx(expected)


def test_cache_savings_positive_on_hit():
    rec = {"virtual_model": "gw-qwen", "input_tokens": 500, "cache_read_tokens": 500}
    # 命中省 = 命中量 × (输入价 - 缓存读价)
    assert pricing.cache_savings([rec], CFG) == pytest.approx(500 * (0.8 - 0.16) / 1e6)


def test_group_by_counts_and_costs():
    recs = [
        {"virtual_model": "gw-qwen", "input_tokens": 1_000_000},
        {"virtual_model": "gw-qwen", "input_tokens": 1_000_000},
        {"virtual_model": "gw-qwen-flash", "input_tokens": 1_000_000},
    ]
    g = pricing.group_by(recs, CFG)
    assert g["gw-qwen"][0] == 2
    assert g["gw-qwen-flash"][0] == 1
