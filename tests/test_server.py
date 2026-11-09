# -*- coding: utf-8 -*-
"""server.py 的轻量单测 —— 锁住任务分组键等纯函数。"""

import server


class _Req:
    def __init__(self, headers=None):
        self.headers = headers or {}


def test_task_group_uses_session_id_when_present():
    req = _Req({"x-claude-code-session-id": "abc123"})
    assert server._task_group(req, "sk-gw-local-dev") == "session:abc123"


def test_task_group_falls_back_to_key():
    req = _Req()
    assert server._task_group(req, "sk-gw-local-dev") == "sk-gw-local-dev"


def test_task_group_blank_session_falls_back_to_key():
    # 头存在但内容全空白 → 视为没有 session，退回按 key
    req = _Req({"x-claude-code-session-id": "   "})
    assert server._task_group(req, "sk-gw-local-dev") == "sk-gw-local-dev"
