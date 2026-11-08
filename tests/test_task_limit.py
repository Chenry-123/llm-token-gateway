# -*- coding: utf-8 -*-
"""task_limit.py 的单元测试 —— 锁住任务级累计 token 上限的边界。"""

import task_limit


def _cfg(tmp_path, max_tokens=100, enabled=True):
    return {
        "_config_dir": str(tmp_path),
        "log": {"path": "logs/usage.jsonl"},
        "task_limit": {"enabled": enabled, "max_tokens": max_tokens},
    }


def test_check_disabled_returns_none(tmp_path):
    cfg = _cfg(tmp_path, enabled=False)
    assert task_limit.check(cfg, "k1", 999) is None


def test_check_zero_cap_returns_none(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=0)
    assert task_limit.check(cfg, "k1", 999) is None


def test_check_under_cap_returns_none(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=100)
    assert task_limit.check(cfg, "k1", 50) is None


def test_check_over_cap_returns_402(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=100)
    result = task_limit.check(cfg, "k1", 150)
    assert result is not None
    assert result[0] == 402


def test_check_counts_accumulated(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=100)
    task_limit.accumulate(cfg, "k1", input_tokens=80, output_tokens=0)
    # 已累计 80 + 本次预估 30 = 110 > 100 → 拦
    result = task_limit.check(cfg, "k1", 30)
    assert result is not None
    assert result[0] == 402


def test_accumulate_and_get_used(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=1000)
    task_limit.accumulate(cfg, "k1", input_tokens=10, output_tokens=20)
    assert task_limit.get_used(cfg, "k1") == 30


def test_accumulate_sums_across_calls(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=1000)
    task_limit.accumulate(cfg, "k1", input_tokens=10, output_tokens=5)
    task_limit.accumulate(cfg, "k1", input_tokens=3, output_tokens=2)
    assert task_limit.get_used(cfg, "k1") == 20


def test_reset_clears(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=1000)
    task_limit.accumulate(cfg, "k1", input_tokens=100, output_tokens=50)
    task_limit.reset(cfg, "k1")
    assert task_limit.get_used(cfg, "k1") == 0


def test_state_persists_across_loads(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=1000)
    task_limit.accumulate(cfg, "k1", input_tokens=7, output_tokens=3)
    # 强制清掉内存缓存，模拟进程重启后从文件重新读
    task_limit._cache = None
    assert task_limit.get_used(cfg, "k1") == 10


def test_different_keys_isolated(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=1000)
    task_limit.accumulate(cfg, "key-a", input_tokens=100, output_tokens=0)
    assert task_limit.get_used(cfg, "key-b") == 0


# ---- status：查询当前任务状态 ----

def test_status_disabled(tmp_path):
    cfg = _cfg(tmp_path, enabled=False)
    st = task_limit.status(cfg, "k1")
    assert st["enabled"] is False
    assert st["remaining_tokens"] is None
    assert st["used_tokens"] == 0


def test_status_reports_used_and_remaining(tmp_path):
    cfg = _cfg(tmp_path, max_tokens=100)
    task_limit.accumulate(cfg, "k1", input_tokens=20, output_tokens=10)
    st = task_limit.status(cfg, "k1")
    assert st["enabled"] is True
    assert st["max_tokens"] == 100
    assert st["used_tokens"] == 30
    assert st["remaining_tokens"] == 70


def test_status_remaining_floor_at_zero(tmp_path):
    # 累计超过上限时，剩余不为负数，钳在 0
    cfg = _cfg(tmp_path, max_tokens=100)
    task_limit.accumulate(cfg, "k1", input_tokens=150, output_tokens=0)
    st = task_limit.status(cfg, "k1")
    assert st["remaining_tokens"] == 0
