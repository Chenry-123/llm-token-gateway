# -*- coding: utf-8 -*-
"""
analyze_tasks.py —— 用日志数据回答「任务上限该设多少」。

要解决的问题：设了任务级 token 上限（task_limit.max_tokens）后，
    会牺牲多少「任务完成率」、又能拦下多少超长任务。

方法：
  1. 读 logs/usage.jsonl，容错解析（字段随版本演进，老日志缺新字段，用默认值兜底）。
  2. 把请求归成「任务」：历史日志没有 session id，用启发式——
     相邻请求时间间隔 <= gap 分钟算同一个任务。
     （启用 task_limit 后的新日志会带 task_used_before，将来可据此更准地还原边界。）
  3. 输出：
     - 单次请求 token 分位数（input+output）
     - 任务累计 token 分位数
     - 「上限 → 完成率」对照表（上限取几个候选值时，多少比例的任务能完整跑完）

用法：
    python analyze_tasks.py                 # 全部计费请求（默认排除 mock 自验）
    python analyze_tasks.py --model gw-qwen # 只看某个模型
    python analyze_tasks.py --gap 10        # 归组间隔（分钟），默认 10

注意（诚实边界）：
    完成率 = 任务累计 token <= 上限 的任务占比。这是「任务 token 分布」的 CDF，
    它能告诉你「设某个上限会拦断百分之多少的任务」，但「拦断」不等于「该任务
    失败了」——手动场景下，任务被拦后你可以 reset 再继续，损失的是「一次跑完的
    体验」，不是任务本身不可完成。
"""

import argparse
import datetime
import json
import os

LOG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "logs", "usage.jsonl"
)


def load_rows(path):
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def percentile(vals, p):
    """线性插值分位数。vals 非空。"""
    vals = sorted(vals)
    k = (len(vals) - 1) * p
    lo = int(k)
    hi = min(lo + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (k - lo)


def _tok(r):
    return int(r.get("input_tokens") or 0) + int(r.get("output_tokens") or 0)


def _ts(r):
    try:
        return datetime.datetime.fromisoformat(r.get("ts") or "").timestamp()
    except (ValueError, TypeError):
        return 0.0


def group_tasks(rows, gap_minutes):
    """按时间排序，相邻间隔 > gap_minutes 就切一个新任务。返回任务 token 列表。"""
    rows = sorted(rows, key=_ts)
    gap = gap_minutes * 60
    tasks = []
    cur = 0
    prev = None
    for r in rows:
        t = _ts(r)
        if prev is not None and t - prev > gap:
            tasks.append(cur)
            cur = 0
        cur += _tok(r)
        prev = t
    if cur:
        tasks.append(cur)
    return tasks


def _fmt_pct(vals):
    return "  ".join("P%d=%.0f" % (int(p * 100), percentile(vals, p)) for p in (0.5, 0.75, 0.9, 0.95, 0.99))


def main():
    ap = argparse.ArgumentParser(description="分析日志里的 token 分布，帮定任务上限")
    ap.add_argument("--model", action="append", default=None, help="只统计某个 virtual_model（可多次）")
    ap.add_argument("--gap", type=int, default=10, help="任务归组的最大间隔分钟数，默认 10")
    ap.add_argument("--path", default=LOG_PATH, help="日志路径")
    args = ap.parse_args()

    rows = load_rows(args.path)
    if not rows:
        print("没有可读的日志。")
        return

    # 只算成功的请求；默认排除 mock 自验（token 是假数据）
    rows = [r for r in rows if r.get("ok")]
    rows = [r for r in rows if not str(r.get("virtual_model", "")).startswith("gw-mock")]
    if args.model:
        rows = [r for r in rows if r.get("virtual_model") in args.model]

    if not rows:
        print("过滤后没有数据。")
        return

    ts_list = [r.get("ts") for r in rows if r.get("ts")]
    print("样本数：%d（已排除 mock 自验和失败请求）" % len(rows))
    if ts_list:
        print("日期范围：%s ~ %s" % (min(ts_list)[:10], max(ts_list)[:10]))

    # 按天分布，帮用户识别「哪几天是批量评测」
    byday = {}
    for r in rows:
        d = (r.get("ts") or "")[:10]
        byday[d] = byday.get(d, 0) + 1
    print("按天分布：%s" % "  ".join("%s:%d" % (d, byday[d]) for d in sorted(byday)))

    per_req = [_tok(r) for r in rows]
    print("\n【单次请求 input+output token 分位数】")
    print("  " + _fmt_pct(per_req) + "  Max=%d" % max(per_req))

    tasks = group_tasks(rows, args.gap)
    print("\n【任务累计 token 分位数】（按间隔 <= %d 分钟归组，共 %d 个任务）" % (args.gap, len(tasks)))
    print("  " + _fmt_pct(tasks) + "  Max=%d" % max(tasks))

    # 上限 → 完成率对照表
    tasks_sorted = sorted(tasks)
    n = len(tasks_sorted)
    candidates = []
    for p in (0.5, 0.75, 0.9, 0.95, 0.99):
        v = round(percentile(tasks_sorted, p))
        if v not in candidates:
            candidates.append(v)
    print("\n【上限 → 完成率】任务累计 token 上限取以下值时，能完整跑完的任务比例：")
    print("  上限 token   完成率      能跑完")
    for cap in candidates:
        done = sum(1 for t in tasks_sorted if t <= cap)
        print("  %10d   %6.1f%%    %d/%d" % (cap, done / n * 100, done, n))

    print("\n【怎么读】")
    print("  - 完成率 = 任务累计 token <= 上限 的任务占比。上限越低越省，但完成率越低。")
    print("  - 甜点通常在 P90~P95 附近：只牺牲 5%~10% 完成率，却能拦下长尾里最长的那些任务。")
    print("  - 归组用的是时间间隔启发式，历史日志没有 session id，不精确；")
    print("    若某天请求是批量评测（一天几百条、token 高度集中），会被并成一个巨大任务，归组失真。")
    print("  - 启用 task_limit 后，新日志会带 task_used_before，届时可据此更准地还原任务边界。")


if __name__ == "__main__":
    main()
