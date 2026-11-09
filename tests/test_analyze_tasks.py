# -*- coding: utf-8 -*-
"""analyze_tasks.py 的单元测试 —— 锁住任务归组逻辑（session 精确归组 vs 时间间隔）。"""

import analyze_tasks


def _row(sid, in_tok, out_tok=0, ts="2026-11-09T10:00:00+08:00"):
    return {
        "session_id": sid,
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "ts": ts,
    }


def test_group_by_session_when_session_id_present():
    rows = [
        _row("s1", 100, 10),
        _row("s1", 200, 20),   # 同一 session，累计
        _row("s2", 50, 5),     # 另一个 session
    ]
    tasks, n_session, n_gap = analyze_tasks.group_tasks(rows, gap_minutes=10)
    assert n_session == 2
    assert n_gap == 0
    # s1: 100+10+200+20 = 330；s2: 50+5 = 55
    assert sorted(tasks) == sorted([330, 55])


def test_group_by_gap_when_no_session_id():
    rows = [
        _row(None, 10, 0, "2026-11-09T10:00:00+08:00"),
        _row(None, 20, 0, "2026-11-09T10:05:00+08:00"),   # 间隔 5 分钟，同任务
        _row(None, 30, 0, "2026-11-09T11:00:00+08:00"),   # 间隔 55 分钟，新任务
    ]
    tasks, n_session, n_gap = analyze_tasks.group_tasks(rows, gap_minutes=10)
    assert n_session == 0
    assert n_gap == 2
    assert sorted(tasks) == sorted([30, 30])


def test_mixed_session_and_gap():
    rows = [
        _row("s1", 100, 0),
        _row("s1", 100, 0),
        _row(None, 10, 0, "2026-11-09T10:00:00+08:00"),
        _row(None, 20, 0, "2026-11-09T10:02:00+08:00"),   # 无 session，同任务
    ]
    tasks, n_session, n_gap = analyze_tasks.group_tasks(rows, gap_minutes=10)
    assert n_session == 1       # s1 一个 session
    assert n_gap == 1           # 无 session 的两条按间隔归成 1 个任务
    assert sorted(tasks) == sorted([200, 30])
