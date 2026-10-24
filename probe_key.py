"""
探针：不经过网关，直接用你自己的 key 问一次百炼。

为什么要单独有这一步：
    接 Qoder 时有三个变量可能出错 —— ① key 无效 ② 网关配置错 ③ Qoder 配置错。
    三个一起动，出问题就分不清是谁的错。
    这个脚本一次只回答①，而且顺带验证②依赖的那件事（流式里能不能拿到用量）。
    selftest.py 用的是假上游，这里用真上游 —— 两者都对上，接 Qoder 时就只剩 Qoder 一个未知数。

它会真的消耗额度：两次调用合计约 30-60 token，qwen-plus-latest 下不到 1 分钱。

用法（在 gateway 目录下）：
    .venv\\Scripts\\python.exe probe_key.py
"""

import json
import os
import sys
import time

import httpx

ENV_NAME = "DASHSCOPE_API_KEY"
BASE = "https://dashscope.aliyuncs.com/compatible-mode/v1"
MODEL = "qwen-plus-latest"


def hr(title):
    print()
    print("=" * 62)
    print(title)
    print("=" * 62)


def main():
    hr("0. key 在不在")
    key = os.environ.get(ENV_NAME, "").strip()

    if not key:
        print(f"[x] 环境变量 {ENV_NAME} 是空的。")
        print()
        print("    如果是刚设的，请【关掉这个窗口，重新开一个】再跑 ——")
        print("    环境变量只对设置之后新开的进程生效，已经开着的窗口读不到。")
        sys.exit(1)

    print(f"[ok] 读到了 {ENV_NAME}：长度 {len(key)}，开头是 {key[:4]}****")
    if not key.startswith("sk-"):
        print("[!] 百炼的 key 一般以 sk- 开头，你这个不是。先确认没复制错。")

    client = httpx.Client(timeout=60)

    hr("1. 非流式问一次 —— 验证 key 本身有效")
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": "只回复两个字：收到"}],
    }
    t0 = time.time()
    try:
        r = client.post(
            f"{BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    except Exception as e:
        print(f"[x] 连不上：{type(e).__name__}: {e}")
        print("    检查网络 / 代理软件（有的代理会拦 HTTPS）。")
        sys.exit(2)
    dt = int((time.time() - t0) * 1000)
    print(f"HTTP {r.status_code}    {dt} ms")

    if r.status_code != 200:
        print()
        print("[x] 上游拒绝了，原文如下：")
        print(r.text[:800])
        print()
        print("常见原因：")
        print("  401 → key 写错了 / 复制时带了空格或换行 / key 已被删")
        print("  403 → 账号还没开通百炼，或没同意服务协议")
        print("  429 → 免费额度用完了，或触发限流")
        sys.exit(3)

    data = r.json()
    print("它回答：", data["choices"][0]["message"]["content"].strip())
    print("usage ：", json.dumps(data.get("usage"), ensure_ascii=False))

    hr("2. 流式问一次 —— 验证网关记账依赖的那件事")
    print("（网关必须在流式里拿到用量，否则就只能退化成数字符）")
    print()

    body2 = dict(body, stream=True, stream_options={"include_usage": True})
    usage_seen = None
    chunks = 0
    t0 = time.time()
    try:
        with client.stream(
            "POST",
            f"{BASE}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body2,
        ) as resp:
            print(f"HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    continue
                chunks += 1
                try:
                    obj = json.loads(payload)
                except Exception:
                    continue
                if obj.get("usage"):
                    usage_seen = obj["usage"]
    except Exception as e:
        print(f"[x] 流出错：{type(e).__name__}: {e}")
        sys.exit(4)

    dt = int((time.time() - t0) * 1000)
    print(f"收到 {chunks} 个数据块，{dt} ms")
    print("最后一个块里的 usage：", json.dumps(usage_seen, ensure_ascii=False))

    hr("结论")
    if usage_seen:
        print("[ok] 两件事都成立：")
        print("     ① key 有效，能正常调通百炼")
        print("     ② 流式结尾确实带回了 usage —— 网关的记账方式在真上游上成立")
        print()
        print(f"     本次消耗：input={usage_seen.get('prompt_tokens')} "
              f"output={usage_seen.get('completion_tokens')}")
        print("     一会儿接完 Qoder，日志里的数字就该和这个长一个样。")
    else:
        print("[!] key 有效，但流式里没拿到 usage。")
        print("    这对网关是坏消息 —— 说明记账得改成按字符数估算。")
        print("    把这个输出发我，我来改。")


if __name__ == "__main__":
    main()
