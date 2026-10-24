# -*- coding: utf-8 -*-
"""dashboard.py —— 成本看板：把网关账本生成一张自包含的 HTML

定位：MCP 壳是给「Claude Code 等客户端」查账的；这个看板是给「人」看的。
读 logs/usage.jsonl（事实）+ config.json（价格），用 pricing.py 把 token 换成钱，
生成一张 dashboard.html —— 数据全部烘焙进去，**双击即看、完全离线、零依赖**。

跑法：  ./.venv/Scripts/python.exe dashboard.py
        （或 run.bat dashboard）
产物：  dashboard.html（在 gateway 目录下，双击用浏览器打开）

为什么不搞成常驻 web 服务：成本看板一周看一两次就够了，不值得为它常开一个进程。
每次想看就重跑一下，生成的 HTML 是静态快照，还能当记录存档、随手转发。
"""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import budget  # noqa: E402
import pricing  # noqa: E402
import server as gw  # noqa: E402
import usage_log  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "dashboard.html")

# 配色（浅色主题，浏览器默认白底）
C = {
    "bg": "#f6f8fb", "card": "#ffffff", "ink": "#0f172a", "muted": "#64748b",
    "line": "#e2e8f0", "accent": "#2563eb", "green": "#059669", "red": "#dc2626",
    "amber": "#d97706",
}
PALETTE = ["#2563eb", "#059669", "#7c3aed", "#d97706", "#db2777", "#0891b2"]


def _fmt(v):
    return "%.4f" % v


def _date_of(ts):
    return (ts or "")[:10] or "?"


def build_data():
    cfg = gw.CONFIG
    records, bad = usage_log.read_all(cfg)
    billable = [r for r in records if r.get("billable") and r.get("ok")]

    total = pricing.total_cost(billable, cfg)
    saved = pricing.cache_savings(billable, cfg)
    cache_read = sum(r.get("cache_read_tokens") or 0 for r in billable)
    cache_write = sum(r.get("cache_write_tokens") or 0 for r in billable)
    in_tok = sum(r.get("input_tokens") or 0 for r in billable)
    out_tok = sum(r.get("output_tokens") or 0 for r in billable)
    routed = sum(1 for r in billable if r.get("routed"))

    by_model = pricing.group_by(billable, cfg)

    # 按天聚合（成本 + 请求数）
    daily = {}
    for r in billable:
        d = _date_of(r.get("ts"))
        daily.setdefault(d, {"cost": 0.0, "n": 0})
        daily[d]["cost"] += pricing.cost_of(r, cfg)
        daily[d]["n"] += 1
    days = sorted(daily)

    # 难度分布（真实记录才有 difficulty 字段）
    diff = {}
    for r in billable:
        k = r.get("difficulty")
        if k:
            diff[k] = diff.get(k, 0) + 1

    # 预算状态
    bc = cfg.get("budget") or {}
    budget_enabled = bool(bc.get("enabled"))
    budget_cap = float(bc.get("cap_yuan") or 0)
    budget_window = bc.get("window") or "total"
    budget_spent = budget.spent(cfg, budget_window) if budget_enabled else total

    # 最近请求
    recent = []
    for r in billable[-15:][::-1]:
        recent.append({
            "ts": (r.get("ts") or "")[5:19],
            "model": r.get("virtual_model") or "?",
            "in": r.get("input_tokens") or 0,
            "out": r.get("output_tokens") or 0,
            "cache_read": r.get("cache_read_tokens") or 0,
            "cost": pricing.cost_of(r, cfg),
            "routed": bool(r.get("routed")),
        })

    return {
        "total": total, "saved": saved, "cache_read": cache_read,
        "cache_write": cache_write, "in_tok": in_tok, "out_tok": out_tok,
        "n_billable": len(billable), "n_total": len(records), "bad": bad,
        "routed": routed, "by_model": by_model, "daily": daily, "days": days,
        "diff": diff, "budget_enabled": budget_enabled, "budget_cap": budget_cap,
        "budget_window": budget_window, "budget_spent": budget_spent,
        "recent": recent, "generated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


# ---------------------------------------------------------------- SVG 图表

def _h_bar(items, width=520, bar_h=30, gap=14):
    """横向条形图：items = [(名称, 成本)]，按成本降序。返回 SVG 字符串。"""
    if not items:
        return '<p style="color:%s">还没有计费记录。</p>' % C["muted"]
    items = sorted(items, key=lambda x: -x[1])
    maxv = items[0][1] or 1e-9
    left = 170
    h = len(items) * (bar_h + gap) + 8
    parts = []
    for i, (name, cost) in enumerate(items):
        y = 8 + i * (bar_h + gap)
        w = max(2, cost / maxv * width)
        color = PALETTE[i % len(PALETTE)]
        parts.append(
            '<text x="0" y="%d" font-size="12" fill="%s">%s</text>'
            % (y + bar_h / 2 + 4, C["ink"], name))
        parts.append(
            '<rect x="%d" y="%d" width="%d" height="%d" rx="4" fill="%s"/>'
            % (left, y, w, bar_h, color))
        parts.append(
            '<text x="%d" y="%d" font-size="12" fill="%s">¥%s</text>'
            % (left + w + 8, y + bar_h / 2 + 4, C["muted"], _fmt(cost)))
    return ('<svg viewBox="0 0 %d %d" width="100%%" height="%d" '
            'xmlns="http://www.w3.org/2000/svg">%s</svg>'
            % (left + width + 110, h, h, "".join(parts)))


def _v_col(days, values, width=640, height=180):
    """竖向柱状图：days 对齐 values，成本。返回 SVG 字符串。"""
    if not days:
        return '<p style="color:%s">还没有计费记录。</p>' % C["muted"]
    n = len(days)
    maxv = max(values) or 1e-9
    plot_h = height - 40
    bw = width / n
    parts = []
    # 基线
    parts.append('<line x1="0" y1="%d" x2="%d" y2="%d" stroke="%s"/>'
                 % (plot_h, width, plot_h, C["line"]))
    for i, (d, v) in enumerate(zip(days, values)):
        x = i * bw + bw * 0.15
        bh = max(2, v / maxv * plot_h)
        y = plot_h - bh
        parts.append('<rect x="%.1f" y="%.1f" width="%.1f" height="%.1f" '
                     'rx="3" fill="%s"/>'
                     % (x, y, bw * 0.7, bh, C["accent"]))
        parts.append('<text x="%.1f" y="%d" font-size="10" fill="%s" '
                     'text-anchor="middle">%s</text>'
                     % (i * bw + bw / 2, plot_h + 14, C["muted"], d[5:]))
        parts.append('<text x="%.1f" y="%.1f" font-size="9" fill="%s" '
                     'text-anchor="middle">¥%s</text>'
                     % (i * bw + bw / 2, y - 4, C["muted"], _fmt(v)))
    return ('<svg viewBox="0 0 %d %d" width="100%%" height="%d" '
            'xmlns="http://www.w3.org/2000/svg">%s</svg>'
            % (width, height, height, "".join(parts)))


def _card(label, value, sub, color):
    return (
        '<div class="card"><div class="k">%s</div>'
        '<div class="v" style="color:%s">%s</div>'
        '<div class="s">%s</div></div>'
        % (label, color, value, sub))


def render(d):
    budget_line = "未开启（不计上限）"
    if d["budget_enabled"]:
        rem = d["budget_cap"] - d["budget_spent"]
        budget_line = "上限 ¥%s · 已花 ¥%s · 剩 ¥%s" % (
            _fmt(d["budget_cap"]), _fmt(d["budget_spent"]), _fmt(max(rem, 0)))

    diff_html = ""
    if d["diff"]:
        order = ["easy", "medium", "hard"]
        chips = "".join(
            '<span class="chip">%s × %d</span>' % (k, d["diff"].get(k, 0))
            for k in order if d["diff"].get(k))
        diff_html = '<div class="section"><h2>难度分布（路由决策）</h2>%s</div>' % chips

    # 最近请求表
    rows = ""
    for r in d["recent"]:
        rows += (
            '<tr><td class="mono">%s</td><td>%s</td>'
            '<td class="num">%s</td><td class="num">%s</td>'
            '<td class="num">%s</td><td class="num">¥%s</td><td>%s</td></tr>'
            % (r["ts"], r["model"], r["in"], r["out"], r["cache_read"],
               _fmt(r["cost"]), "路由" if r["routed"] else ""))
    recent_html = (
        '<div class="section"><h2>最近请求（计费）</h2>'
        '<table><thead><tr><th>时间</th><th>模型</th><th>输入</th><th>输出</th>'
        '<th>缓存读</th><th>成本</th><th></th></tr></thead>'
        '<tbody>%s</tbody></table></div>' % rows)

    by_model_items = [(name, cost) for name, (_, cost) in d["by_model"].items()]
    daily_values = [d["daily"][x]["cost"] for x in d["days"]]

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>token 网关 · 成本看板</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: %(bg)s; color: %(ink)s; padding: 28px; line-height: 1.5; }
  .wrap { max-width: 860px; margin: 0 auto; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: %(muted)s; font-size: 13px; margin-bottom: 20px; }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr));
           gap: 14px; margin-bottom: 22px; }
  .card { background: %(card)s; border: 1px solid %(line)s; border-radius: 12px;
          padding: 16px 18px; }
  .card .k { font-size: 12px; color: %(muted)s; }
  .card .v { font-size: 26px; font-weight: 700; margin: 4px 0; }
  .card .s { font-size: 12px; color: %(muted)s; }
  .section { background: %(card)s; border: 1px solid %(line)s; border-radius: 12px;
             padding: 18px 20px; margin-bottom: 18px; }
  .section h2 { font-size: 15px; margin-bottom: 14px; }
  table { width: 100%%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid %(line)s; }
  th { color: %(muted)s; font-weight: 500; font-size: 12px; }
  .num, .mono { font-variant-numeric: tabular-nums; }
  .mono { font-family: "SF Mono", Consolas, monospace; font-size: 12px; color: %(muted)s; }
  .chip { display: inline-block; background: %(bg)s; border: 1px solid %(line)s;
          border-radius: 999px; padding: 2px 10px; font-size: 12px; margin-right: 8px; }
  .foot { color: %(muted)s; font-size: 12px; text-align: center; margin-top: 6px; }
</style></head><body><div class="wrap">
  <h1>token 网关 · 成本看板</h1>
  <div class="sub">数据从 logs/usage.jsonl 读（只记事实），成本按 config.json 当前单价现算 ·
  生成于 %(generated)s</div>

  <div class="cards">
    %(card_total)s
    %(card_saved)s
    %(card_requests)s
    %(card_tokens)s
  </div>

  <div class="section"><h2>按模型成本（成本按实际计费模型 billed_model 算）</h2>
    %(by_model)s</div>

  <div class="section"><h2>每日成本趋势</h2>%(daily)s</div>

  %(diff)s

  %(recent)s

  <div class="section"><h2>预算闸门</h2>
    <div style="font-size:14px">%(budget)s</div>
    <div style="color:%(muted)s;font-size:12px;margin-top:6px">路由命中 %(routed)d 次 ·
    日志共 %(n_total)d 行（坏行 %(bad)d）· 计费 %(n_billable)d 条</div>
  </div>

  <div class="foot">token 网关 · 本地生成 · 数据不上云 · 双击 dashboard.html 即可查看</div>
</div></body></html>""" % {
        "bg": C["bg"], "card": C["card"], "ink": C["ink"], "muted": C["muted"],
        "line": C["line"], "generated": d["generated"],
        "card_total": _card("总成本（计费）", "¥" + _fmt(d["total"]),
                            "缓存净省 ¥" + _fmt(d["saved"]), C["accent"]),
        "card_saved": _card("缓存净节省", "¥" + _fmt(d["saved"]),
                            "命中 %s token" % d["cache_read"], C["green"]),
        "card_requests": _card("计费请求", str(d["n_billable"]) + " 次",
                               "路由命中 %d 次" % d["routed"], C["ink"]),
        "card_tokens": _card("Token 用量", "%s 入 / %s 出" % (d["in_tok"], d["out_tok"]),
                             "缓存写 %s" % d["cache_write"], C["muted"]),
        "by_model": _h_bar(by_model_items),
        "daily": _v_col(d["days"], daily_values),
        "diff": diff_html,
        "recent": recent_html,
        "budget": budget_line,
        "routed": d["routed"], "n_total": d["n_total"], "bad": d["bad"],
        "n_billable": d["n_billable"],
    }
    return html


def main():
    d = build_data()
    html = render(d)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write(html)
    print("已生成：%s" % OUT)
    print("总成本 ¥%s · 计费 %d 条 · 缓存净省 ¥%s"
          % (_fmt(d["total"]), d["n_billable"], _fmt(d["saved"])))


if __name__ == "__main__":
    main()
