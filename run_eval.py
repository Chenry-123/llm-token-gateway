# -*- coding: utf-8 -*-
"""
run_eval.py —— 跑评测：多轮对话 + 记成本 + 打分

这是「评测」的最小闭环：读 evalset.json 的任务，通过网关跑多轮对话
（第 2、3 轮带历史 → 触发上游缓存），对每轮回答打分（judge.py），
最后输出每个任务的「成本 + 质量分」。

成本从【网关日志】读，不自己从响应 usage 估 —— 因为路由会把 easy 请求的
实际模型换成便宜档，只有日志里的 billed_model 才知道真相。

支持四种模式（环境变量 GW_ROUTING / GW_CACHE 控制，可叠加）：
  GW_ROUTING 不设 / =0    → 基线（纯透传 + 隐式缓存），输出 eval_baseline.json
  GW_CACHE=1              → 打开显式缓存，输出 eval_cache.json
  GW_ROUTING=1            → 打开模型路由，输出 eval_routing.json
  GW_ROUTING=1 GW_CACHE=1 → 路由 + 显式缓存叠加，输出 eval_stack.json

四个文件放一起，就是完整的成本-质量帕累托曲线。

注意：这会真实消耗 token 和约 25 分钟（35 题 × 3 轮 = 105 次调用，每题还有一次裁判打分）。
"""

import asyncio
import json
import os
import time

if not os.environ.get("DASHSCOPE_API_KEY"):
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
    v, _ = winreg.QueryValueEx(k, "DASHSCOPE_API_KEY")
    os.environ["DASHSCOPE_API_KEY"] = v

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import httpx
import uvicorn

import judge
import pricing
import server as gw
import usage_log

MODEL = "gw-qwen"   # 客户端请求的模型名（对外名）。路由/基线都走这个入口。

# 路由开关：GW_ROUTING=1 时，把 config 里各模型的 routing.enabled 置 True。
# 改的是内存里的 gw.CONFIG，不碰 config.json 文件 —— 基线跑法一字不变。
ROUTING = os.environ.get("GW_ROUTING") == "1"

# 显式缓存开关：GW_CACHE=1 时，把 cache.explicit 置 True（网关给 system 打 cache_control）。
# 这是无损优化：输出不变，只是缓存从「不确定命中的隐式」变成「确定命中的显式」。
CACHE = os.environ.get("GW_CACHE") == "1"

EVALSET_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evalset.json")


def _cost_of_last_billable(cfg, before_count):
    """从网关日志读「本次请求」产生的成本。

    成本必须从日志读，因为：
      1. 路由会把 easy 请求的真实模型换成便宜档，响应里的 usage 只告诉你
         token 数，不告诉你这些 token 该按谁的价格算。
      2. 日志里 billed_model 记着「成本按谁算」的真相。
    只在 before_count 之后的记录里找 billable 的那条（应该正好一条）。
    """
    records, _bad = usage_log.read_all(cfg)
    for r in records[before_count:]:
        if r.get("billable"):
            return pricing.cost_of(r, cfg), (r.get("cache_read_tokens") or 0), r.get("routed")
    return 0.0, 0, False


def _log_count(cfg):
    return len(usage_log.read_all(cfg)[0])


async def run_task(client, base, gw_key, task, system, cfg):
    """跑一个任务的多轮对话，返回每一轮的 {question, answer, cost, cache_read, routed}。"""
    messages = [{"role": "system", "content": system}]
    turns = []
    for user_msg in task["user_turns"]:
        messages.append({"role": "user", "content": user_msg})
        before = _log_count(cfg)
        r = await client.post(
            base + "/v1/chat/completions",
            headers={"authorization": "Bearer " + gw_key, "content-type": "application/json"},
            json={"model": MODEL, "messages": messages, "max_tokens": 512},
        )
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        answer = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")
        messages.append({"role": "assistant", "content": answer})
        cost, cache_read, routed = _cost_of_last_billable(cfg, before)
        turns.append({
            "question": user_msg,
            "answer": answer,
            "cost": cost,
            "cache_read": cache_read,
            "routed": routed,
        })
    return turns


async def main():
    with open(EVALSET_PATH, encoding="utf-8") as f:
        evalset = json.load(f)
    system = evalset["system"]
    tasks = evalset["tasks"]

    cfg = gw.CONFIG
    if ROUTING:
        for m in cfg.get("models", {}).values():
            if isinstance(m.get("routing"), dict):
                m["routing"]["enabled"] = True
    if CACHE:
        cfg.setdefault("cache", {})["explicit"] = True

    listen = cfg["listen"]
    base = "http://%s:%d" % (listen["host"], listen["port"])
    gw_key = (cfg.get("gateway_keys") or ["sk-gw-local-dev"])[0]

    srv = uvicorn.Server(uvicorn.Config(gw.app, host=listen["host"], port=listen["port"], log_level="warning"))
    srv_task = asyncio.create_task(srv.serve())
    t0 = time.time()
    while not srv.started:
        if time.time() - t0 > 20:
            raise RuntimeError("网关启动超时")
        await asyncio.sleep(0.05)

    results = []
    async with httpx.AsyncClient(timeout=180.0, trust_env=False) as c:
        for task in tasks:
            turns = await run_task(c, base, gw_key, task, system, cfg)

            total_score = 0
            covered_union = [False] * len(task["key_points"])
            for turn in turns:
                res = judge.judge(turn["question"], task["key_points"], turn["answer"])
                total_score += res.get("score", 0)
                for k in range(len(task["key_points"])):
                    if k < len(res.get("covered", [])) and res["covered"][k]:
                        covered_union[k] = True

            task_cost = sum(t["cost"] for t in turns)
            avg_score = total_score / len(turns) if turns else 0
            covered_count = sum(covered_union)
            cache_read_total = sum(t["cache_read"] for t in turns)
            routed_count = sum(1 for t in turns if t["routed"])
            results.append({
                "id": task["id"],
                "difficulty": task["difficulty"],
                "topic": task["topic"],
                "cost": task_cost,
                "avg_score": round(avg_score, 2),
                "covered": covered_count,
                "total_points": len(task["key_points"]),
                "cache_read": cache_read_total,
                "routed_turns": routed_count,
            })
            print("  [%s] %-8s %-12s 成本 %.6f 元 | 主观 %.1f | 要点 %d/%d | 缓存读 %d | 路由 %d轮"
                  % (task["id"], task["difficulty"], task["topic"], task_cost,
                     avg_score, covered_count, len(task["key_points"]), cache_read_total, routed_count))

    srv.should_exit = True
    await asyncio.gather(srv_task, return_exceptions=True)
    await asyncio.sleep(0.2)

    parts = []
    parts.append("路由开（easy→qwen-flash）" if ROUTING else "纯透传")
    parts.append("显式缓存" if CACHE else "隐式缓存")
    mode_label = " + ".join(parts)
    print()
    print("=" * 72)
    print("汇总（%s，%s）" % (MODEL, mode_label))
    print("=" * 72)
    total_cost = sum(r["cost"] for r in results)
    n = len(results)
    avg_subj = sum(r["avg_score"] for r in results) / n if n else 0
    avg_cov = 100.0 * sum(r["covered"] for r in results) / sum(r["total_points"] for r in results) if n else 0
    total_routed = sum(r["routed_turns"] for r in results)
    total_cache_read = sum(r["cache_read"] for r in results)
    print("任务数 %d | 总成本 %.6f 元 | 平均主观分 %.2f | 平均要点覆盖 %.0f%% | 被路由 %d 轮 | 缓存读 %d"
          % (n, total_cost, avg_subj, avg_cov, total_routed, total_cache_read))

    out = {
        "model": MODEL,
        "routing": ROUTING,
        "explicit_cache": CACHE,
        "note": mode_label,
        "total_cost": total_cost,
        "avg_subjective_score": round(avg_subj, 2),
        "avg_keypoint_coverage": round(avg_cov, 1),
        "total_routed_turns": total_routed,
        "total_cache_read_tokens": total_cache_read,
        "results": results,
    }
    if CACHE and ROUTING:
        fname = "eval_stack.json"
    elif CACHE:
        fname = "eval_cache.json"
    elif ROUTING:
        fname = "eval_routing.json"
    else:
        fname = "eval_baseline.json"
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fname)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("结果已存：%s" % out_path)


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
