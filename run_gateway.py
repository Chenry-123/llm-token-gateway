# -*- coding: utf-8 -*-
"""
run_gateway.py —— 常驻启动网关（接真实客户端时用）

和 run.bat 的分工：run.bat 适合「当前 shell 已经能读到 DASHSCOPE_API_KEY」时用；
本脚本多干一件事 —— 从注册表把上游 key 捞进内存再启动，**不依赖环境变量继承**。

为什么要它：Windows 下用 [Environment]::SetEnvironmentVariable 设的用户级环境变量，
只在「新开的 shell」里生效；后台/守护进程、双击启动的进程常常拿不到旧快照。
网关启动时若读不到 key，症状是上游 401，极难自查。所以干脆从注册表直接读。

用法：python run_gateway.py   （或双击 run.bat 也一样，二者选一）
"""

import os

# 1. 先把上游 key 从注册表捞进环境（必须在 import server 之前：server 运行时读 os.environ）
if not os.environ.get("DASHSCOPE_API_KEY"):
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
        v, _ = winreg.QueryValueEx(k, "DASHSCOPE_API_KEY")
        os.environ["DASHSCOPE_API_KEY"] = v
    except FileNotFoundError:
        pass

# 2. 保护回环地址不被本机代理吞掉（必须在 import server 前，理由同 selftest）
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import uvicorn

import server

if __name__ == "__main__":
    cfg = server.CONFIG["listen"]
    print("网关启动：http://%s:%d  （Ctrl+C 停止）" % (cfg["host"], cfg["port"]))
    uvicorn.run(server.app, host=cfg["host"], port=cfg["port"], log_level="info")
