# -*- coding: utf-8 -*-
"""
reset_task.py —— 和网关交互「任务状态」：查进度 / 清零累计。

配合 task_limit（config.json 的 task_limit 块）使用：

    python reset_task.py status   # 查当前任务：累计多少 token、离上限还差多少
    python reset_task.py reset    # 清零，开始新任务（不传参数默认就是 reset）

它自己会从 config.json 读网关地址和 gateway_keys，不需要你记端口或 key。
前提：网关得在运行中（先双击 run.bat 启动）。
"""

import json
import os
import sys
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    with open(os.path.join(_HERE, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def _gateway():
    cfg = _load_config()
    listen = cfg.get("listen") or {}
    host = listen.get("host") or "127.0.0.1"
    port = listen.get("port") or 8317
    keys = [k for k in (cfg.get("gateway_keys") or []) if k]
    key = keys[0] if keys else "sk-gw-local-dev"
    return "http://%s:%d" % (host, port), key


def _show_status(base, key):
    url = base + "/v1/task/status"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + key})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print("查询失败（HTTP %d）：%s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        return
    except urllib.error.URLError as e:
        print("连不上网关：%s" % e.reason)
        print("请先双击 run.bat 启动网关，再跑 run.bat status。")
        return

    if not data.get("enabled"):
        print("任务累计上限未开启（config.json 里 task_limit.enabled=false）。")
        print("当前已累计 %d token（不计上限）。" % data.get("used_tokens", 0))
        return

    print("任务累计状态：")
    print("  已累计   %d token" % data.get("used_tokens", 0))
    print("  上限     %d token" % data.get("max_tokens", 0))
    rem = data.get("remaining_tokens")
    if rem is not None:
        print("  剩余     %d token（%.0f%%）" % (rem, (1 - data["used_tokens"] / data["max_tokens"]) * 100 if data.get("max_tokens") else 0))
    print("  开始于   %s" % (data.get("started_at") or "（还未开始，累计为 0）"))


def _do_reset(base, key):
    url = base + "/v1/task/reset"
    req = urllib.request.Request(
        url, method="POST", headers={"Authorization": "Bearer " + key}
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        print("任务已重置：")
        print("  之前已累计 %d token" % data.get("used_before", 0))
        print("  现在归零，新任务从 0 开始计。")
    except urllib.error.HTTPError as e:
        print("重置失败（HTTP %d）：%s" % (e.code, e.read().decode("utf-8", "replace")[:200]))
        print("请确认 config.json 里 gateway_keys 包含：%s" % key)
    except urllib.error.URLError as e:
        print("连不上网关：%s" % e.reason)
        print("请先双击 run.bat 启动网关，再跑 run.bat reset。")


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "reset"
    if action not in ("status", "reset"):
        print("用法：python reset_task.py [status|reset]，默认 reset。")
        return
    base, key = _gateway()
    if action == "status":
        _show_status(base, key)
    else:
        _do_reset(base, key)


if __name__ == "__main__":
    main()
