# -*- coding: utf-8 -*-
"""
usage_log.py —— 记账层

三条规矩，全部来自第 02 课：

  1. 只记事实，不记派生值。
     日志里没有"花了多少钱"，只有 token 数。因为单价会变 ——
     今天 80 元/百万 token，明年可能 20 元。记了成本，那批数据
     就永远没法用新单价重算。

  2. 只记统计量，不记内容。
     记消息条数、字符数、有没有图片；不记你实际问了什么、模型答了什么。
     这样这份日志可以**安全公开** —— 我们要开源，这一条是前提。

  3. 追加写，不覆盖；一行一条 jsonl。
     坏一行只丢一行，还能边跑边写。
"""

import json
import os
from datetime import datetime, timezone

# 日志里一行长这样（字段名就是"事实"的名字）：
#
#   ts                     什么时候
#   request_id             这一次调用的编号
#   route / protocol       从哪条路由进来、属于哪个协议
#   client_hint            客户端用的哪种认证头（用来分辨 Qoder 还是 Claude Code）
#   virtual_model          客户端请求的模型名（网关对外暴露的名字）
#   upstream_model         真正发给上游的模型名
#   upstream_host          上游主机（不记完整 URL，避免把 key 写进日志）
#   stream                 是不是流式
#   ok / status            成功吗、上游返回什么状态码
#   latency_ms             端到端耗时
#   input_tokens           输入 token
#   output_tokens          输出 token
#   cache_read_tokens      缓存命中的输入 token（按 0.1 倍价计费）
#   cache_write_tokens     写入缓存的输入 token（按 1.25 倍价计费）
#   usage_reported         这个数字是上游给的，还是我们自己数的
#   output_chars           模型实际吐了多少字符（用来交叉验证上面的数字）
#   error_kind             失败分类（auth / rate_limit / timeout / ...）
#   retry_count            重试了几次
#   req_*                  请求的统计量（条数、字符数、工具数、有无图片）
#
# 注意最后一行：**没有任何一个字段装着请求原文。**


def log_path(config):
    """日志文件的绝对路径。配置里写的是相对路径时，相对于配置文件所在目录。"""
    cfg = config.get("log") or {}
    p = cfg.get("path") or "logs/usage.jsonl"
    if os.path.isabs(p):
        return p
    base = config.get("_config_dir") or os.getcwd()
    return os.path.join(base, p)


def append(config, record):
    """追加一条。返回这条记录的绝对路径，方便调用方打印出来。"""
    path = log_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    record.setdefault("ts", datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"))

    with open(path, "a", encoding="utf-8") as f:
        # ensure_ascii=False：否则中文会变成 \uXXXX 转义，人读不了
        f.write(json.dumps(record, ensure_ascii=False) + "\n")

    return path


def read_all(config):
    """读回全部记录。

    返回 (records, bad_lines) —— **坏行跳过并计数，不让一条坏数据毁掉整批**。
    这是第 02 课埋进去的那个设计：日志文件是会长大的，你不可能保证
    历史数据永远干净（比如进程被杀在写入中途）。
    """
    path = log_path(config)
    if not os.path.exists(path):
        return [], 0

    records, bad = [], 0
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return records, bad


def reset(config):
    """清空日志。

    注意用的是 `open(path, "w").close()` 而不是 `os.remove(path)` ——
    不真删文件，只把它截成空。这样既清干净了，也不会碰到
    "删除文件"这类需要额外权限的操作。
    """
    path = log_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").close()
    return path
