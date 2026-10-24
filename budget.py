# -*- coding: utf-8 -*-
"""
budget.py —— 预算闸门（把「降本」从「让每次更便宜」补全到「不让总量失控」）

缓存和路由降的是**单价**（每百万 token 多少钱）；但单价再低，也拦不住
「次数太多 / 单次输入太大」把总账推上去。预算闸门管的是**总量**，两层：

  1. 硬上限：窗口内累计花费 >= 上限 → 直接拒（402），一个 token 都不再放。
  2. 预检：发请求前，按「本次输入全价、无缓存」的最坏情况估一笔，
     若「已花 + 本次最坏输入成本」会超过上限 → 也拒。这一层专防
     「一次喂超大上下文直接冲爆预算」，默认关闭（见下）。

设计原则（和 pricing.py 一致，守住它）：
    日志只记 token 事实，钱在读日志时才算。所以这里的「已花」= 把日志里
    所有计费记录用【当前单价】重算一遍的和。今天改了单价，历史也会按新价重算。

窗口（window）：
    "total"  从日志第一行算起的累计（默认，最简单、无时区歧义）
    "daily"  从今天零点算起（适合「每天最多花多少」的直觉）
"""

import datetime

import pricing
import usage_log


def _window_start(window):
    """该窗口的起始时刻。total 无起始 → 返回 None。daily → 今天零点（本地、带时区）。"""
    if window == "daily":
        # 用带时区的 now：日志里的 ts 是「2026-09-22T23:39:57+08:00」这种带时区的，
        # naive 和 aware 直接比会抛 TypeError，所以这里必须先变成 aware。
        now = datetime.datetime.now().astimezone()
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    return None


def _in_window(record, start):
    """一条记录在不在窗口内。没有时间戳的旧记录按「在窗口内」处理（宁多算不漏）。"""
    if start is None:
        return True
    ts = record.get("ts")
    if not ts:
        return True
    try:
        dt = datetime.datetime.fromisoformat(ts)
    except ValueError:
        return True
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=start.tzinfo)   # 旧日志若没带时区，按本地时间算
    return dt >= start


def spent(config, window="total"):
    """窗口内已经花了多少钱（元）。

    只算计费记录（billable=True）。失败/不计费的那些没有 token 字段，
    pricing.cost_of 对它们自然返回 0，不用在这里单独过滤 —— 但过滤掉更干净。
    """
    records, _bad = usage_log.read_all(config)
    start = _window_start(window)
    billable = [r for r in records if r.get("billable") and _in_window(r, start)]
    return pricing.total_cost(billable, config)


def _input_price_per_token(config, model):
    """某模型每 token 的输入单价（元）。没配价返回 None。"""
    spec = (config.get("models") or {}).get(model) or {}
    p = spec.get("price_per_million") or {}
    if not p or "input" not in p:
        return None
    return float(p["input"]) / 1_000_000


def estimate_input_cost(config, model, total_input_chars):
    """本次请求输入的最坏成本估计（元）。

    用 total_input_chars // 4 粗估 token 数（与 server.py 流式估算同一个系数，
    口径一致），再按**全价输入**算 —— 故意不算缓存折扣，因为缓存命不命中
    在发请求前是不知道的。返回 None = 这个模型没配价、估不了（此时预检放行，
    只靠硬上限兜底）。
    """
    price = _input_price_per_token(config, model)
    if price is None:
        return None
    tokens = max(1, total_input_chars // 4)
    return tokens * price


def check(config, *, billed_model, total_input_chars):
    """预算闸门的唯一入口。

    返回 None = 放行；返回 (status, message) = 拦截，调用方拿这个造错误响应。

    只认 billed_model（路由后实际计费的模型）—— 钱按谁算，预算就按谁估。
    计费与否（billable）由调用方判断：count_tokens 这类不花钱的端点不该被拦。
    """
    bc = config.get("budget") or {}
    if not bc.get("enabled"):
        return None

    cap = float(bc.get("cap_yuan") or 0)
    if cap <= 0:
        return None            # 没设上限 = 不拦

    window = bc.get("window") or "total"
    already = spent(config, window)

    # 第 1 层 · 硬上限：已经花超了，直接拒
    if already >= cap:
        return (
            402,
            "预算已用尽：%s 窗口内累计花费 ¥%.2f，已达上限 ¥%.2f。"
            "要恢复使用，请调高 config.json 里的 budget.cap_yuan，"
            "或把 window 改成 daily 等下一个自然日重置。"
            % (window, already, cap),
        )

    # 第 2 层 · 预检：防单次超大输入冲爆预算（默认关闭，见 config 说明）
    if bc.get("preflight", True):
        est = estimate_input_cost(config, billed_model, total_input_chars)
        if est is not None and already + est >= cap:
            return (
                402,
                "本次请求会超预算：已花 ¥%.2f，本次输入最坏约 ¥%.4f，"
                "合计将达上限 ¥%.2f。请缩短上下文后重试，或调高 budget.cap_yuan。"
                % (already, est, cap),
            )

    return None
