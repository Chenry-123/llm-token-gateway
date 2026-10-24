# -*- coding: utf-8 -*-
"""
mcp_server.py —— MCP 壳：把网关的「账本查询」暴露成 MCP 工具

定位（和网关的分工）：
    省钱能力在**网关**（夹在客户端和厂商之间，强制执行），
    MCP 是「插件形态」的外壳 —— 让 Claude Code / Cursor 等客户端
    能主动**查询**「我花了多少钱、预算还剩多少、钱花在哪」。

它不碰网关进程，只读两份本地文件：
    config.json（价格表 + 预算设置） + logs/usage.jsonl（网关记的账），
    用 pricing.py 把 token 换成钱。所以**网关不在线也能查历史账**。

为什么用 fastmcp 而不是手写 MCP 协议：
    MCP 的 STDIO + JSON-RPC 帧是纯协议样板，跟业务无关，手写是重复造轮子；
    用官方 fastmcp 只留「@mcp.tool 一行一个工具」，把精力花在工具本身的
    业务逻辑上（查成本 / 查预算 / 查用量）。

启动（Claude Code 里）：
    claude mcp add gateway -- python <本文件路径>
    或写进 .mcp.json。transport 是 stdio。
"""

import os
import sys

# 保证无论从哪个目录启动，都能 import 到同目录的 server/pricing 等模块
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP  # noqa: E402

import budget  # noqa: E402
import pricing  # noqa: E402
import server as gw  # noqa: E402  （只为拿到 CONFIG：价格表、预算、日志路径）
import usage_log  # noqa: E402

mcp = FastMCP("token-gateway")


def _billable(cfg):
    """计费且成功的记录（失败/不计费端点不算钱）。"""
    records, _bad = usage_log.read_all(cfg)
    return [r for r in records if r.get("billable") and r.get("ok")]


def _fmt_yuan(v):
    return "%.4f" % v


@mcp.tool
def cost_summary() -> str:
    """查当前累计成本：总额、按模型分组、token 用量、缓存省了多少。

    无参数。读网关日志 + 价格表，返回可读的文本摘要。
    """
    cfg = gw.CONFIG
    billable = _billable(cfg)
    total = pricing.total_cost(billable, cfg)
    by_model = pricing.group_by(billable, cfg)
    saved = pricing.cache_savings(billable, cfg)

    lines = ["累计成本摘要（共 %d 条计费记录）" % len(billable)]
    lines.append("总成本：¥%s" % _fmt_yuan(total))
    lines.append("")
    lines.append("按模型分组：")
    for name, (cnt, cost) in by_model.items():
        share = cost / total * 100 if total else 0
        lines.append("  %-16s %4d 次  ¥%-10s (%.1f%%)"
                     % (name, cnt, _fmt_yuan(cost), share))
    lines.append("")
    lines.append("缓存省了：¥%s %s" % (_fmt_yuan(saved),
                                       "（命中缓存按缓存价计费）" if saved >= 0 else "（当前缓存写比输入贵）"))
    return "\n".join(lines)


@mcp.tool
def budget_status() -> str:
    """查预算闸门状态：是否开启、上限、已花多少、还剩多少。

    无参数。预算设置读 config.json 的 budget 段；已花金额按当前单价重算历史日志。
    """
    cfg = gw.CONFIG
    bc = cfg.get("budget") or {}
    enabled = bool(bc.get("enabled"))

    if not enabled:
        already = pricing.total_cost(_billable(cfg), cfg)
        return ("预算闸门未开启（config.json 里 budget.enabled=false）。\n"
                "当前累计成本：¥%s（不计上限，放心跑）" % _fmt_yuan(already))

    cap = float(bc.get("cap_yuan") or 0)
    window = bc.get("window") or "total"
    already = budget.spent(cfg, window)
    remaining = cap - already

    lines = ["预算闸门状态（窗口=%s）" % window]
    lines.append("上限：¥%s" % _fmt_yuan(cap))
    lines.append("已花：¥%s" % _fmt_yuan(already))
    if remaining > 0:
        lines.append("剩余：¥%s" % _fmt_yuan(remaining))
    else:
        lines.append("剩余：¥0.0000（已用尽，新请求会被网关 402 拦下）")
    return "\n".join(lines)


@mcp.tool
def recent_usage(n: int = 10) -> str:
    """查最近 n 条请求的用量摘要：时间、模型、输入/输出 token、成本。

    参数 n：返回最近几条，默认 10。
    """
    cfg = gw.CONFIG
    records, _bad = usage_log.read_all(cfg)
    records = [r for r in records if r.get("billable")]
    recent = records[-max(1, min(n, 50)):]

    if not recent:
        return "还没有计费记录。"

    lines = ["最近 %d 条计费请求：" % len(recent)]
    for r in recent:
        cost = pricing.cost_of(r, cfg)
        lines.append(
            "%s  %-16s  入 %s 出 %s  ¥%s%s"
            % (r.get("ts", "")[:19],
               r.get("virtual_model") or "?",
               r.get("input_tokens") or 0,
               r.get("output_tokens") or 0,
               _fmt_yuan(cost),
               "  [路由]" if r.get("routed") else ""),
        )
    return "\n".join(lines)


if __name__ == "__main__":
    # 默认 stdio transport，给 Claude Code 等 MCP 客户端挂载用
    mcp.run()
