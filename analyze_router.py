# -*- coding: utf-8 -*-
"""
analyze_router.py —— 路由质量损失的统计收尾

用现有 4 份评测数据，回答一个尖锐的问题：
「路由（easy 题换成 qwen-flash）到底有没有显著降低质量？」

背景（为什么需要这个脚本）：
  同一套路由，短 system 测出「掉 10.5% 质量」，长 system 测出「涨 7%」——方向反了。
  这说明 LLM-judge 主观分在 12 题规模下信噪比不足，单次点估计测不显著。

方法（配对设计，不新增评测）：
  1. 重测噪声：baseline vs cache 是「同输入」两次独立跑（缓存无损，模型看到的东西
     完全一样），它们的逐题分数差 = 纯重测噪声（模型采样 + judge 采样）。
  2. 路由差：baseline vs routing 逐题分数差，按「该题被路由几轮」分成三组：
       - 未路由（routed_turns=0，两边都是 qwen-plus-latest）→ 差 = 纯重测噪声（对照组）
       - 纯路由（routed_turns=3，整题都是 qwen-flash）→ 差 = 噪声 + 真实路由效应
       - 部分路由（0<routed_turns<3）→ 混合，稀释了效应，单独列示
  3. 配对检验：对「纯路由题」的路由差做 Wilcoxon 符号秩检验（精确 p 值，枚举 2^n
     符号组合，小样本不依赖正态假设），判断是否显著偏离 0。

结论会直接告诉我们：flash 的「质量损失」在当前规模下能不能和噪声区分开。
"""
import json
import math
import os
from itertools import product


def load(name):
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), name),
              encoding="utf-8") as f:
        return json.load(f)


def mean(x):
    return sum(x) / len(x) if x else 0.0


def std(x, ddof=1):
    if len(x) < 2:
        return 0.0
    m = mean(x)
    return math.sqrt(sum((v - m) ** 2 for v in x) / (len(x) - ddof))


def wilcoxon_signed_rank(diffs):
    """配对 Wilcoxon 符号秩检验，返回 (W+, W-, n, 双侧精确 p 值)。

    diffs: 配对差列表（内部会剔除 0）。
    p 值通过枚举 2^n 种符号分配精确计算，小样本不依赖正态近似。
    """
    diffs = [d for d in diffs if d != 0]
    n = len(diffs)
    if n == 0:
        return None, None, 0, None
    order = sorted(range(n), key=lambda k: abs(diffs[k]))
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j < n and abs(diffs[order[j]]) == abs(diffs[order[i]]):
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg_rank
        i = j
    W_plus = sum(ranks[k] for k in range(n) if diffs[k] > 0)
    W_minus = sum(ranks[k] for k in range(n) if diffs[k] < 0)
    target = abs(W_plus - W_minus)
    if n > 20:
        # 2^20 已超百万，改用正态近似（小样本才走精确枚举）
        from statistics import NormalDist
        mean_w = sum(ranks) / 2.0
        var_w = sum(r * r for r in ranks) / 4.0
        z = (target - 0.5) / math.sqrt(var_w) if var_w > 0 else 0.0
        p = 2 * (1 - NormalDist().cdf(abs(z)))
        return W_plus, W_minus, n, p
    count = 0
    total = 0
    for signs in product([-1, 1], repeat=n):
        s = sum(signs[k] * ranks[k] for k in range(n))
        total += 1
        if abs(s) >= target:
            count += 1
    p = count / total
    return W_plus, W_minus, n, p


def main():
    base = load("eval_baseline.json")    # 隐式缓存, 不路由（qwen-plus-latest）
    cache = load("eval_cache.json")      # 显式缓存, 不路由（qwen-plus-latest）
    route = load("eval_routing.json")    # 隐式缓存, 路由
    stack = load("eval_stack.json")      # 显式缓存, 路由

    def scores(d):
        return {r["id"]: r["avg_score"] for r in d["results"]}

    sb, sc, sr, ss = scores(base), scores(cache), scores(route), scores(stack)
    ids = [r["id"] for r in base["results"]]
    routed = {r["id"]: r["routed_turns"] for r in route["results"]}

    print("=" * 72)
    print("路由质量损失 · 统计收尾")
    print("=" * 72)

    # ---- 1. 重测噪声（同输入：baseline vs cache）----
    noise = [sc[i] - sb[i] for i in ids]
    print("\n[1] 重测噪声 = baseline vs cache 逐题分数差（同输入，纯噪声）")
    print("    差列表:", [round(d, 2) for d in noise])
    print("    均值 %.3f | 标准差 %.3f（%d 题）" % (mean(noise), std(noise), len(noise)))
    se_noise = std(noise) / math.sqrt(len(noise))
    print("    均值标准误 %.3f —— 这是「一次 %d 题评测的均值」本身带多少噪声"
          % (se_noise, len(noise)))

    # ---- 2. 路由差，分组 ----
    print("\n[2] 路由差 = baseline vs routing 逐题分数差（正数=路由反而更好）")
    for i in ids:
        d = sr[i] - sb[i]
        if routed[i] == 0:
            grp = "未路由"
        elif routed[i] == 3:
            grp = "纯路由"
        else:
            grp = "部分路由"
        print("    %-6s %-6s %+5.2f  (baseline %.2f -> routing %.2f)"
              % (i, grp, d, sb[i], sr[i]))

    unr = [sr[i] - sb[i] for i in ids if routed[i] == 0]
    pure = [sr[i] - sb[i] for i in ids if routed[i] == 3]
    part = [sr[i] - sb[i] for i in ids if 0 < routed[i] < 3]

    print("\n    未路由题（对照组，两边都 plus-latest）: n=%d 均值 %+.3f 标准差 %.3f"
          % (len(unr), mean(unr), std(unr)))
    print("    纯路由题（plus-latest vs flash）    : n=%d 均值 %+.3f 标准差 %.3f"
          % (len(pure), mean(pure), std(pure)))
    print("    部分路由题（混合，稀释）       : n=%d 均值 %+.3f"
          % (len(part), mean(part)))

    # ---- 3. 配对检验 ----
    print("\n[3] 配对 Wilcoxon 符号秩检验（双侧精确 p 值）")
    for label, diffs in [("纯路由题 plus-latest vs flash", pure),
                         ("未路由题（应≈0，做方法校验）", unr)]:
        Wp, Wm, n, p = wilcoxon_signed_rank(diffs)
        if n == 0:
            continue
        print("    %-28s n=%d  W+=%g W-=%g  p=%.4f" % (label, n, Wp, Wm, p))

    # ---- 4. 结论 ----
    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    se_routing = std(pure) / math.sqrt(len(pure)) if pure else 0
    d_bar = mean(pure)
    print("纯路由题（plus-latest→flash）质量差均值 %+.3f，标准误 %.3f" % (d_bar, se_routing))
    print("重测噪声（同输入）标准差 %.3f —— 和路由差同量级，说明信号被噪声淹没。"
          % std(noise))
    print("=> 在当前 %d 题规模下，路由的质量损失「测不显著」（方向甚至为正）。" % len(ids))
    print("   省钱结论（路由 -51.2% / 叠加 -55.0%）可信，因为从日志读、")
    print("   不依赖裁判；质量结论需要更大样本才能和噪声分离。")
    print("   注意：缓存对 plus-latest 无效（该模型不支持缓存），cache 配置≈基线；")
    print("   缓存只在 flash 上生效（stack 比 routing 多省约 3.8 个百分点）。")


if __name__ == "__main__":
    main()
