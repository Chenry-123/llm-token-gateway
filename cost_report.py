# -*- coding: utf-8 -*-
"""
cost_report.py —— 从日志算成本，打印一份报告

用法：python cost_report.py

它不碰网关进程，只读日志文件 + config，用 pricing.py 把 token 换成钱。
这是「测量」的第一块：先知道现在花了多少、花在哪，才有资格谈「省了多少」。

只算【计费且成功】的记录。失败的和 count_tokens 这类不计费端点，不算钱
（失败没产生真实用量，count_tokens 本来就不计费）。
"""

import server as gw
import pricing
import usage_log


def main():
    cfg = gw.CONFIG
    records, bad = usage_log.read_all(cfg)

    billable = [r for r in records if r.get("billable") and r.get("ok")]

    total = pricing.total_cost(billable, cfg)
    by_model = pricing.group_by(billable, cfg)

    print("日志：%s" % usage_log.log_path(cfg))
    print("共 %d 条记录，其中计费且成功 %d 条，坏行 %d 条" % (len(records), len(billable), bad))
    print()
    print("总成本：%.6f 元" % total)
    print()
    print("%-24s %8s %14s %8s" % ("模型（对外名）", "次数", "成本(元)", "占比"))
    print("-" * 58)
    for name, (cnt, cost) in by_model.items():
        share = cost / total * 100 if total else 0
        print("%-24s %8d %14.6f %7.1f%%" % (name, cnt, cost, share))

    # 汇总一下 token 量，让「钱」和「量」对得上，方便你去控制台核对
    total_in = sum(r.get("input_tokens") or 0 for r in billable)
    total_out = sum(r.get("output_tokens") or 0 for r in billable)
    total_cr = sum(r.get("cache_read_tokens") or 0 for r in billable)
    total_cw = sum(r.get("cache_write_tokens") or 0 for r in billable)
    print()
    print("token 合计：输入 %d / 输出 %d / 缓存读 %d / 缓存写 %d"
          % (total_in, total_out, total_cr, total_cw))

    # 「缓存省了多少」—— 这是降本的第一枪
    saved = pricing.cache_savings(billable, cfg)
    print()
    print("=" * 58)
    print("缓存省了多少：%.6f 元" % saved)
    if saved > 0:
        print("（命中的缓存 token 按缓存价计费，比按输入价便宜了这么多）")
    elif saved < 0:
        print("（缓存写比输入贵，当前写入的缓存反而让账单变大）")
    print("=" * 58)


if __name__ == "__main__":
    main()
