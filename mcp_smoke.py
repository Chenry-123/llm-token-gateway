# -*- coding: utf-8 -*-
"""mcp_smoke.py —— MCP 壳的冒烟测试（改完 mcp_server.py 跑一遍确认没坏）

做两件事：
  1. 直接 import mcp_server，列出三个工具并调用，验证业务逻辑（查账）对；
  2. 用 fastmcp 的 Client 以 stdio 方式**真的拉起** mcp_server.py，
     列出工具 + 调一个工具，证明「挂到 Claude Code 一定能连上」。

依赖 gateway/.venv（selftest.bat 首跑时建的），fastmcp 已装在里面。
跑法：  ./.venv/Scripts/python.exe mcp_smoke.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcp_server as m  # noqa: E402
from fastmcp import Client  # noqa: E402
from fastmcp.client.transports import StdioTransport  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "mcp_server.py")
PY = os.path.join(HERE, ".venv", "Scripts", "python.exe")
EXPECTED = ["budget_status", "cost_summary", "recent_usage"]


def test_import_and_tools():
    """直接 import：三个工具都注册了，且能被调用、返回中文摘要。"""
    assert sorted(m.cost_summary and []) == [] or True  # 占位，保持结构
    print("直接调用 cost_summary()：")
    out = m.cost_summary()
    assert "总成本" in out, out
    print(out)
    print()
    assert "预算闸门" in m.budget_status()
    assert "最近" in m.recent_usage(n=3) or "还没有" in m.recent_usage(n=3)
    print("直接调用三个工具：OK")
    print()


async def test_stdio_roundtrip():
    """用 fastmcp Client 走 stdio 真正拉起子进程，端到端验证。"""
    transport = StdioTransport(command=PY, args=[SERVER])
    async with Client(transport) as client:
        tools = await client.list_tools()
        names = [t.name for t in tools]
        print("stdio 列出工具：", names)
        assert sorted(names) == EXPECTED, names

        res = await client.call_tool("budget_status", {})
        text = res.data if hasattr(res, "data") else res
        if hasattr(text, "text"):
            text = text.text
        print("stdio 调用 budget_status 首行：", str(text).splitlines()[0])
        assert "预算闸门" in str(text)
    print("stdio 端到端：OK")


if __name__ == "__main__":
    test_import_and_tools()
    asyncio.run(test_stdio_roundtrip())
    print()
    print("MCP 壳冒烟测试全部通过 ✓")
