# -*- coding: utf-8 -*-
"""router.py 的单元测试 —— 锁住难度边界和「同厂商降档」守卫。"""

import router


def test_empty_messages_is_medium():
    assert router.guess_difficulty({"messages": []}) == "medium"


def test_short_text_is_easy():
    body = {"messages": [{"role": "user", "content": "写个函数"}]}
    assert router.guess_difficulty(body) == "easy"


def test_len_140_is_easy():
    body = {"messages": [{"role": "user", "content": "x" * 140}]}
    assert router.guess_difficulty(body) == "easy"


def test_len_141_is_medium():
    body = {"messages": [{"role": "user", "content": "x" * 141}]}
    assert router.guess_difficulty(body) == "medium"


def test_len_280_is_hard():
    body = {"messages": [{"role": "user", "content": "x" * 280}]}
    assert router.guess_difficulty(body) == "hard"


def test_hard_hint_wins_over_length():
    # 很短但命中难词，应判 hard 而不是 easy
    body = {"messages": [{"role": "user", "content": "帮我设计一个高并发架构"}]}
    assert router.guess_difficulty(body) == "hard"


def test_extract_text_str():
    assert router._extract_text("hello") == "hello"


def test_extract_text_anthropic_blocks():
    blocks = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert router._extract_text(blocks) == "a b"


def test_last_user_text_skips_assistant():
    body = {"messages": [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]}
    assert router._last_user_text(body) == "second"


def test_pick_upstream_disabled_passthrough():
    mc = {"upstream_model": "qwen-plus-latest"}
    body = {"messages": [{"role": "user", "content": "hi"}]}
    upstream, billed, routed, diff = router.pick_upstream(mc, {}, body)
    assert upstream == "qwen-plus-latest"
    assert billed is None
    assert routed is False
    assert diff is None


def test_pick_upstream_easy_same_vendor_switches():
    mc = {
        "upstream_model": "qwen-plus-latest",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "protocol": "openai",
        "routing": {"enabled": True, "cheap_model": "gw-qwen-flash"},
    }
    models = {
        "gw-qwen-flash": {
            "upstream_model": "qwen-flash",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
            "protocol": "openai",
        }
    }
    body = {"messages": [{"role": "user", "content": "hi"}]}
    upstream, billed, routed, diff = router.pick_upstream(mc, models, body)
    assert upstream == "qwen-flash"
    assert billed == "gw-qwen-flash"
    assert routed is True
    assert diff == "easy"


def test_pick_upstream_cross_vendor_guarded():
    # 便宜模型是别家厂商（协议/key/域名都不同）→ 守卫拦住，不换
    mc = {
        "upstream_model": "qwen-plus-latest",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "protocol": "openai",
        "routing": {"enabled": True, "cheap_model": "gw-claude"},
    }
    models = {
        "gw-claude": {
            "upstream_model": "claude-sonnet-4-6",
            "base_url": "https://api.anthropic.com",
            "api_key_env": "ANTHROPIC_API_KEY",
            "protocol": "anthropic",
        }
    }
    body = {"messages": [{"role": "user", "content": "hi"}]}
    upstream, billed, routed, diff = router.pick_upstream(mc, models, body)
    assert upstream == "qwen-plus-latest"
    assert routed is False


def test_pick_upstream_hard_not_switched():
    mc = {
        "upstream_model": "qwen-plus-latest",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key_env": "DASHSCOPE_API_KEY",
        "protocol": "openai",
        "routing": {"enabled": True, "cheap_model": "gw-qwen-flash"},
    }
    models = {
        "gw-qwen-flash": {
            "upstream_model": "qwen-flash",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "api_key_env": "DASHSCOPE_API_KEY",
            "protocol": "openai",
        }
    }
    body = {"messages": [{"role": "user", "content": "帮我设计一个高并发架构"}]}
    upstream, billed, routed, diff = router.pick_upstream(mc, models, body)
    assert upstream == "qwen-plus-latest"
    assert routed is False
    assert diff == "hard"
