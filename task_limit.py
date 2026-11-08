# -*- coding: utf-8 -*-
"""
task_limit.py —— 任务级累计 token 上限（手动任务）

和 token_limit.py 的分工：
    token_limit.py 管「单次请求」的 token 数（这一条消息的输入/输出各一个硬顶）。
    本模块管「一个任务」累计用了多少 token：从用户手动「开始一个任务」
    （reset）到下一次 reset 之间，所有请求的 input + output 累计不能超过上限。

任务边界：由用户手动控制。
    网关按「客户端 key」累计当前任务的 token，用户开始新任务前调
    POST /v1/task/reset 清零。这样网关无需理解「哪些请求属于同一个对话」，
    边界完全由人定 —— 手动场景（Qoder 手动对话）下最干净。

累计口径（用户拍板）：input + output 全算（含缓存命中），
    对齐「这次任务总共让模型处理/生成了多少 token」。

状态：持久化到 logs/task_state.json（进程重启不丢）。每个 key 一行：
    {"<gateway_key>": {"tokens": 12345, "started_at": "2026-11-08 15:37:05"}}

拦 / 累加的时机（在 server.py 里接入）：
    转发前 check()：已累计 + 本次输入估算（加权）> 上限 → 拦（HTTP 402）。
        只估输入侧，因为输出 token 转发前无法可靠预知（max_tokens 可能没设），
        输出侧靠 token_limit.clamp_output 的单次钳制兜底 + 转发后真值累加。
    转发后 accumulate()：拿上游 usage 真值，把 input + output 累进当前任务。
"""

import json
import os
import threading
import time

# 模块级状态缓存 + 锁。手动对话并发极低，锁只是为了「读-改-写」不被交错。
_lock = threading.Lock()
_cache = None


def _state_path(config):
    """状态文件放哪：跟日志同一目录（logs/），叫 task_state.json。"""
    log_rel = (config.get("log") or {}).get("path") or "logs/usage.jsonl"
    log_dir = os.path.dirname(log_rel)
    return os.path.join(config.get("_config_dir", ""), log_dir, "task_state.json")


def _load(config):
    global _cache
    p = _state_path(config)
    try:
        with open(p, "r", encoding="utf-8") as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        _cache = {}
    return _cache


def _save(config):
    p = _state_path(config)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(_cache or {}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def get_used(config, key):
    """返回该 key 当前任务已累计的 token 数。"""
    with _lock:
        st = _load(config)
        return int((st.get(key) or {}).get("tokens") or 0)


def check(config, key, estimated_incoming):
    """转发前判断：已累计 + 本次预估是否超上限。超了返回 (402, message)，否则 None。"""
    tc = config.get("task_limit") or {}
    if not tc.get("enabled"):
        return None
    max_tokens = tc.get("max_tokens") or 0
    if max_tokens <= 0:
        return None

    used = get_used(config, key)
    if used + int(estimated_incoming or 0) > max_tokens:
        return (
            402,
            "本任务已累计 %d token，本次输入预估 %d token，合计将超过任务上限 %d。"
            "请调高 config.json 里的 task_limit.max_tokens，"
            "或调 POST /v1/task/reset 结束当前任务、开始新任务。"
            % (used, int(estimated_incoming or 0), max_tokens),
        )
    return None


def accumulate(config, key, input_tokens, output_tokens):
    """转发后把上游 usage 的真值累进当前任务，并持久化。"""
    with _lock:
        st = _load(config)
        cur = st.get(key) or {"tokens": 0, "started_at": None}
        added = int(input_tokens or 0) + int(output_tokens or 0)
        st[key] = {
            "tokens": int(cur.get("tokens") or 0) + added,
            "started_at": cur.get("started_at") or _now(),
        }
        _save(config)


def reset(config, key):
    """清零该 key 的累计 —— 用户手动开始新任务的入口。"""
    with _lock:
        st = _load(config)
        st.pop(key, None)
        _save(config)


def status(config, key):
    """查该 key 当前任务的累计状态（只读，给查询端点 / 脚本用）。"""
    tc = config.get("task_limit") or {}
    enabled = bool(tc.get("enabled"))
    max_tokens = tc.get("max_tokens") or 0
    with _lock:
        st = _load(config)
        entry = st.get(key) or {}
    used = int(entry.get("tokens") or 0)
    remaining = None
    if enabled and max_tokens > 0:
        remaining = max(0, max_tokens - used)
    return {
        "enabled": enabled,
        "max_tokens": max_tokens,
        "used_tokens": used,
        "remaining_tokens": remaining,
        "started_at": entry.get("started_at"),
    }
