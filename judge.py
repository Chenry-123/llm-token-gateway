# -*- coding: utf-8 -*-
"""
judge.py —— 打分器：LLM 裁判 + 客观要点覆盖

质量分拆两半，因为只有一半是「软」的：
  主观分：裁判模型对回答整体打分（1-5）—— 软，有偏见
  客观分：裁判逐个判断「参考要点」覆盖了几个 —— 相对硬，能抓「答得通顺但漏了关键」

为什么必须有客观分：降本优化（压缩/路由）真正的损失常常是「模型忘了前面的关键约束」，
回答看着通顺、裁判可能打高分，但漏了要点。光有主观分测不出这种损失。

裁判模型第一版用 qwen-plus 自己 —— 「自己评自己」的偏见是已知限制，
后续换更强模型（qwen-max）或双裁判取均值。

裁判调用直接走百炼、不经过网关：裁判是评测工具，它的调用成本不该混进
「被评测对象」的成本里。
"""

import json
import os

if not os.environ.get("DASHSCOPE_API_KEY"):
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
    v, _ = winreg.QueryValueEx(k, "DASHSCOPE_API_KEY")
    os.environ["DASHSCOPE_API_KEY"] = v

import httpx

BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
JUDGE_MODEL = "qwen-plus"

_PROMPT = """你是评测裁判。下面给出「用户问题」「参考答案要点」「模型回答」。请评估模型回答的质量。

【用户问题】
{question}

【参考答案要点】（共 {n} 个）
{points}

【模型回答】
{answer}

请只输出一个 JSON 对象，不要输出任何其他文字，格式：
{{"score": 3, "covered": [true, false, true]}}

字段含义：
- score：整体质量分，整数 1-5（5=完全正确且完整，1=错误或答非所问）
- covered：数组，长度必须等于要点个数（{n}），每个元素是布尔值，表示该要点是否被模型回答覆盖到

只输出 JSON。"""


def _parse_json(text):
    """从模型输出里抠出 JSON。模型可能包一层 ```json 或加尾巴，容错处理。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    i = t.find("{")
    j = t.rfind("}")
    if i != -1 and j != -1 and j > i:
        try:
            return json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            pass
    return None


def judge(question, key_points, answer):
    """给「问题 + 要点 + 回答」打分。返回 {"score": int, "covered": [bool...]}。"""
    points_text = "\n".join("%d. %s" % (i + 1, p) for i, p in enumerate(key_points))
    prompt = _PROMPT.format(
        question=question, n=len(key_points), points=points_text, answer=answer,
    )
    body = {
        "model": JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 256,
        "stream": False,
    }
    r = httpx.post(
        BASE,
        headers={"Authorization": "Bearer " + os.environ["DASHSCOPE_API_KEY"]},
        json=body,
        timeout=60,
    )
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    content = (j.get("choices") or [{}])[0].get("message", {}).get("content", "")

    parsed = _parse_json(content) if content else None
    if not parsed:
        return {"score": 0, "covered": [False] * len(key_points), "raw": content[:120]}

    score = parsed.get("score")
    covered = parsed.get("covered")
    if not isinstance(score, int):
        score = 0
    if not isinstance(covered, list):
        covered = [False] * len(key_points)
    covered = list(covered)[:len(key_points)] + [False] * (len(key_points) - len(covered))
    return {"score": score, "covered": [bool(c) for c in covered]}
