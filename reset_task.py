# -*- coding: utf-8 -*-
"""
reset_task.py —— 调网关的 /v1/task/reset 端点，清零当前任务的累计 token。

配合 task_limit（config.json 的 task_limit 块）使用：你在开始一个新任务前跑一次
这个脚本（或双击 run.bat reset），网关就把「已累计」归零，重新计。

它自己会从 config.json 读网关地址和 gateway_keys，不需要你记端口或 key。
前提：网关得在运行中（先双击 run.bat 启动）。
"""

import json
import os
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _load_config():
    with open(os.path.join(_HERE, "config.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    cfg = _load_config()
    listen = cfg.get("listen") or {}
    host = listen.get("host") or "127.0.0.1"
    port = listen.get("port") or 8317
    keys = [k for k in (cfg.get("gateway_keys") or []) if k]
    key = keys[0] if keys else "sk-gw-local-dev"

    url = "http://%s:%d/v1/task/reset" % (host, port)
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


if __name__ == "__main__":
    main()
