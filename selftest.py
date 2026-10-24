# -*- coding: utf-8 -*-
"""
selftest.py —— 自验脚本：不需要任何真实 API key，跑通全链路

它做三件事：

  1. 在本机起一个**假上游**（假装自己是厂商），两种协议都支持
  2. 在本机起**真的网关**（就是 server.py）
  3. 用客户端身份打「两条路由 × 流式/非流式」四种组合，再读回日志对账

为什么非要有这个假上游：
    没有它，你得先申请到真 key 才能验证任何东西。
    而这套代码里最容易错的地方（流式用量的落点）**跟上游是谁无关** ——
    用假上游验一遍，真上游那边就只剩"key 对不对"这一个问题。
    这是把「两个不确定因素」拆成「一个已解决 + 一个待确认」。

怎么跑：双击 run.bat test，或 .venv\\Scripts\\python.exe selftest.py
"""

import asyncio
import json
import os
import sys
import time

# ！必须在 `import server` 之前设 —— server 模块在导入时就建好了自己的 httpx 客户端，
#   而 httpx 是在建客户端的那一刻读取代理环境变量的。
#
# 为什么要设：环境里若存在 HTTP_PROXY 且它**不绕过回环地址**，
#   httpx（trust_env 默认为 True）会把访问 127.0.0.1 的请求也交给代理，
#   代理再以「绝对形式」把请求转发回网关（GET http://127.0.0.1:8317/v1/models），
#   结果全链路 404 —— 明明是测试环境的坑，看起来却像网关的路由坏了。
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

import server as gw
import usage_log
import budget
import convert

MOCK_PORT = 8399

# 假上游返回的固定数字 —— 固定下来才能"对账"。
# 这几个数字故意都不一样（123 / 45 / 100 / 7），
# 这样一旦字段串位（比如把缓存读当成了输入），一眼就能看出来。
MOCK_INPUT = 123
MOCK_OUTPUT = 45
MOCK_CACHE_READ = 100
MOCK_CACHE_WRITE = 7
MOCK_TEXT = ["网关", "测试", "成功"]


# =====================================================================
# 假上游
# =====================================================================
mock = FastAPI()


def _openai_usage_json():
    return {
        "prompt_tokens": MOCK_INPUT,
        "completion_tokens": MOCK_OUTPUT,
        "total_tokens": MOCK_INPUT + MOCK_OUTPUT,
        "prompt_tokens_details": {"cached_tokens": MOCK_CACHE_READ},
    }


def _anthropic_usage_json():
    return {
        "input_tokens": MOCK_INPUT,
        "output_tokens": MOCK_OUTPUT,
        "cache_read_input_tokens": MOCK_CACHE_READ,
        "cache_creation_input_tokens": MOCK_CACHE_WRITE,
    }


@mock.post("/chat/completions")
async def mock_openai(request: Request):
    """模拟一家 OpenAI 兼容厂商。

    关键行为：**只有请求里带了 stream_options.include_usage=true，
    它才会在 [DONE] 之前多发一个"只有 usage、choices 为空"的 chunk。**
    这正是真厂商的行为，也是我们"必须注入那个参数"的原因。
    如果网关忘了注入，这里的 usage 就不会出现，自验会发现。
    """
    body = await request.json()
    model = body.get("model")
    want_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    if not body.get("stream"):
        if body.get("tools"):
            # 带了 tools 就假装「模型决定调工具」，返回 tool_calls ——
            # 这样网关的 anthropic→openai 转换能不能把 tool_calls 翻回 tool_use，
            # 自验里就能测到。
            return JSONResponse({
                "id": "chatcmpl-mock", "object": "chat.completion", "model": model,
                "choices": [{
                    "index": 0, "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant", "content": None,
                        "tool_calls": [{
                            "id": "call_mock_1", "type": "function",
                            "function": {"name": "read_file",
                                         "arguments": "{\"path\":\"main.py\"}"},
                        }],
                    },
                }],
                "usage": _openai_usage_json(),
            })
        return JSONResponse({
            "id": "chatcmpl-mock", "object": "chat.completion", "model": model,
            "choices": [{
                "index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "".join(MOCK_TEXT)},
            }],
            "usage": _openai_usage_json(),
        })

    async def gen():
        if body.get("tools"):
            # 流式工具调用：tool_calls 分片累积（第一个 chunk 带 id/name，
            # 后续只带 arguments 碎片），模拟真厂商的行为。
            yield "data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [
                    {"index": 0, "id": "call_mock_1", "type": "function",
                     "function": {"name": "read_file", "arguments": ""}}]},
                    "finish_reason": None}],
            }, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(0.01)
            yield "data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "{\"path\":"}}]}}],
            }, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(0.01)
            yield "data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": "\"main.py\"}"}}]}}],
            }, ensure_ascii=False) + "\n\n"
            yield "data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            }, ensure_ascii=False) + "\n\n"
            if want_usage:
                yield "data: " + json.dumps({
                    "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                    "choices": [],
                    "usage": _openai_usage_json(),
                }, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"
            return

        for t in MOCK_TEXT:
            yield "data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                "choices": [{"index": 0, "delta": {"content": t}, "finish_reason": None}],
            }, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(0.01)

        yield "data: " + json.dumps({
            "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }, ensure_ascii=False) + "\n\n"

        if want_usage:
            yield "data: " + json.dumps({
                "id": "chatcmpl-mock", "object": "chat.completion.chunk", "model": model,
                "choices": [],                    # 注意：空数组
                "usage": _openai_usage_json(),
            }, ensure_ascii=False) + "\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@mock.post("/v1/messages")
async def mock_anthropic(request: Request):
    """模拟 Anthropic 官方。

    关键行为：用量**分两处给**，不需要请求里带任何开关。
      message_start → 输入（含缓存 token）
      message_delta → 输出（累计值）
    """
    body = await request.json()
    model = body.get("model")

    if not body.get("stream"):
        return JSONResponse({
            "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
            "content": [{"type": "text", "text": "".join(MOCK_TEXT)}],
            "stop_reason": "end_turn", "stop_sequence": None,
            "usage": _anthropic_usage_json(),
        })

    async def gen():
        def ev(name, obj):
            return "event: %s\ndata: %s\n\n" % (name, json.dumps(obj, ensure_ascii=False))

        u = _anthropic_usage_json()
        yield ev("message_start", {
            "type": "message_start",
            "message": {
                "id": "msg_mock", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": dict(u, output_tokens=0),
            },
        })
        yield ev("content_block_start", {
            "type": "content_block_start", "index": 0,
            "content_block": {"type": "text", "text": ""},
        })
        for t in MOCK_TEXT:
            yield ev("content_block_delta", {
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": t},
            })
            await asyncio.sleep(0.01)
        yield ev("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield ev("message_delta", {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": MOCK_OUTPUT},
        })
        yield ev("message_stop", {"type": "message_stop"})

    return StreamingResponse(gen(), media_type="text/event-stream")


@mock.post("/v1/messages/count_tokens")
async def mock_count_tokens(request: Request):
    """Claude Code 发消息前会先问这个。它不计费，但**必须能通过**。"""
    return JSONResponse({"input_tokens": MOCK_INPUT})


# =====================================================================
# 驱动
# =====================================================================
CHECKS = []


def check(name, ok, detail=""):
    CHECKS.append((name, bool(ok), detail))
    print("  %s  %-46s %s" % ("[OK]" if ok else "[!!]", name, detail))


async def _wait_started(srv, timeout=20.0):
    t0 = time.time()
    while not srv.started:
        if time.time() - t0 > timeout:
            raise RuntimeError("服务启动超时")
        await asyncio.sleep(0.05)


GROW = "gw-mock-openai"
GCLAUDE = "gw-mock-anthropic"


async def _case(client, base, key, *, route, model, stream, auth_style, label,
                expect_text=True):
    """跑一个用例，检查 HTTP 层的东西。日志层的检查放到最后统一做。"""
    url = base + route
    headers = {"content-type": "application/json"}
    if auth_style == "bearer":
        headers["authorization"] = "Bearer " + key
    else:
        headers["x-api-key"] = key
        headers["anthropic-version"] = "2023-06-01"

    payload = {"model": model, "messages": [{"role": "user", "content": "你好，这是一次自验"}]}
    if stream:
        payload["stream"] = True
    if route.endswith("/v1/messages"):
        payload["max_tokens"] = 256

    t0 = time.perf_counter()
    async with client.stream("POST", url, headers=headers, json=payload) as r:
        body = ""
        async for chunk in r.aiter_text():
            body += chunk
    ms = int((time.perf_counter() - t0) * 1000)

    if r.status_code != 200:
        check(label, False, "HTTP %s：%s" % (r.status_code, body[:160]))
        return

    if expect_text:
        # ！注意这里是"逐个片段找"，不是"找整句"。为什么？
        #   流式返回里，三个词是装在三个独立 chunk 里的 ——
        #   "网关测试成功"这六个字在报文里**根本不连续出现**。
        #   这一条本身就是「流式」的定义：内容是被切开发送的。
        #   （第一版我写成了找整句，结果两条流式用例全挂，
        #     但日志里的用量明明是准的 —— 说明是检测方法错了，不是代码错了。）
        flat = body.replace("\n", "")
        missing = [t for t in MOCK_TEXT if t not in flat]
        if missing:
            check(label, False, "返回内容里缺 %s" % missing)
            return

    check(label, True, "HTTP 200，%d ms，%d 字节" % (ms, len(body)))


def _test_budget_unit():
    """预算闸门的纯逻辑单测：不碰 HTTP、不碰共享日志，用临时目录。

    覆盖：spent 只算计费、硬上限、预检、daily 窗口、开关/上限为空时放行。
    这些是「账算得对不对」的地基，跟网络无关，先单独验干净。
    """
    import datetime
    import tempfile

    print()
    print("=" * 74)
    print("第 0 部分 · 预算闸门纯逻辑单测（临时日志，不影响真实账本）")
    print("=" * 74)

    with tempfile.TemporaryDirectory() as tmp:
        log_file = os.path.join(tmp, "usage.jsonl")
        cfg = {
            "log": {"path": log_file},
            "models": {
                "gw-x": {"price_per_million": {"input": 1.0, "output": 2.0,
                                               "cache_read": 0.2, "cache_write": 1.0}},
            },
            "budget": {"enabled": True, "window": "total", "cap_yuan": 0.005, "preflight": False},
        }
        # 一条计费记录：input 1000 → 1000 * 1.0/1e6 = ¥0.001
        rec = {"billable": True, "virtual_model": "gw-x", "billed_model": "gw-x",
               "input_tokens": 1000, "output_tokens": 0,
               "cache_read_tokens": 0, "cache_write_tokens": 0}

        # 1) spent 只算计费记录，不计费的即使 token 数很大也忽略
        usage_log.append(cfg, dict(rec))
        usage_log.append(cfg, dict(rec, billable=False, input_tokens=999999))
        check("预算·spent 只算计费记录（不计费的忽略）",
              abs(budget.spent(cfg, "total") - 0.001) < 1e-9,
              "¥%.6f" % budget.spent(cfg, "total"))

        # 2) 硬上限：未超放行，超了 402
        check("预算·未超上限时放行",
              budget.check(cfg, billed_model="gw-x", total_input_chars=0) is None, "")
        cfg["budget"]["cap_yuan"] = 0.0005
        reject = budget.check(cfg, billed_model="gw-x", total_input_chars=0)
        check("预算·硬上限：已花 ¥0.001 ≥ 上限 ¥0.0005 → 402",
              reject is not None and reject[0] == 402 and "已用尽" in reject[1],
              reject[1] if reject else "")

        # 3) 预检：清空账本，上限较大但输入超大 → 预检拦
        usage_log.reset(cfg)
        cfg["budget"].update({"cap_yuan": 0.01, "preflight": True})
        reject = budget.check(cfg, billed_model="gw-x", total_input_chars=40000)  # 10000 token × 1e-6 = ¥0.01
        check("预算·预检：单次输入最坏会撞上限 → 402",
              reject is not None and reject[0] == 402 and "会超预算" in reject[1],
              reject[1] if reject else "")

        # 4) daily 窗口：昨天的不算，今天的算
        yesterday = (datetime.datetime.now() - datetime.timedelta(days=1)).isoformat(timespec="seconds")
        usage_log.reset(cfg)
        usage_log.append(cfg, dict(rec, ts=yesterday))
        check("预算·daily 窗口：昨天的花费不计入今天",
              abs(budget.spent(cfg, "daily")) < 1e-9,
              "¥%.6f" % budget.spent(cfg, "daily"))
        usage_log.append(cfg, dict(rec))
        check("预算·daily 窗口：今天的计入",
              abs(budget.spent(cfg, "daily") - 0.001) < 1e-9,
              "¥%.6f" % budget.spent(cfg, "daily"))

        # 5) 开关/上限为空 → 一律放行
        cfg["budget"] = {"enabled": False}
        check("预算·enabled=false 放行",
              budget.check(cfg, billed_model="gw-x", total_input_chars=999999) is None, "")
        cfg["budget"] = {"enabled": True, "cap_yuan": 0}
        check("预算·cap_yuan=0 视为不设上限，放行",
              budget.check(cfg, billed_model="gw-x", total_input_chars=999999) is None, "")


def _test_convert_image():
    """协议转换的图片块单测：image block 不再静默丢弃，翻成 image_url part。

    纯函数，不碰 HTTP。这是「Claude Code 贴图/读图」这条真实路径的格式层验证。
    """
    print()
    print("=" * 74)
    print("第 0.5 部分 · 协议转换图片块单测（纯函数，无 HTTP）")
    print("=" * 74)

    PNG = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="}}

    def _conv(body):
        return convert.anthropic_request_to_openai(body, "qwen-plus")

    # 1) 文本 + 图片 → parts 数组（text 在前、image_url 在后），data URL 拼对
    out = _conv({"model": "claude-sonnet-4-5", "max_tokens": 100,
                 "messages": [{"role": "user", "content": [{"type": "text", "text": "看图"}, PNG]}]})
    c = out["messages"][0]["content"]
    check("图片·文本+图片 → parts 数组，text 在前 image_url 在后",
          isinstance(c, list) and len(c) == 2
          and c[0] == {"type": "text", "text": "看图"}
          and c[1]["type"] == "image_url"
          and c[1]["image_url"]["url"] == "data:image/png;base64,aGVsbG8=", "")

    # 2) 纯图片 → 只有 image_url part，无 text
    out = _conv({"model": "claude-sonnet-4-5", "max_tokens": 100,
                 "messages": [{"role": "user", "content": [PNG]}]})
    c = out["messages"][0]["content"]
    check("图片·纯图片 → 只有 image_url part",
          isinstance(c, list) and len(c) == 1 and c[0]["type"] == "image_url", "")

    # 3) tool_result 里带图片 → role=tool 的 content 也翻成 parts 数组
    out = _conv({"model": "claude-sonnet-4-5", "max_tokens": 100,
                 "messages": [{"role": "user", "content": [
                     {"type": "tool_result", "tool_use_id": "t1",
                      "content": [{"type": "text", "text": "结果"}, PNG]}]}]})
    m = out["messages"][0]
    check("图片·tool_result 带图 → role=tool + parts 数组",
          m["role"] == "tool" and m["tool_call_id"] == "t1"
          and isinstance(m["content"], list) and len(m["content"]) == 2
          and m["content"][1]["type"] == "image_url", "")

    # 4) 纯文本 → 仍是字符串（回归：别把 text-only 请求也误改成数组）
    out = _conv({"model": "claude-sonnet-4-5", "max_tokens": 100,
                 "messages": [{"role": "user", "content": "你好"}]})
    check("图片·纯文本仍是字符串（不误改成数组）",
          out["messages"][0]["content"] == "你好", "")


async def main():
    _test_budget_unit()
    _test_convert_image()

    cfg = gw.CONFIG
    listen = cfg["listen"]
    base = "http://%s:%d" % (listen["host"], listen["port"])
    key = (cfg.get("gateway_keys") or ["sk-gw-local-dev"])[0]

    # 不自验前清空日志 —— 万一你已经用它记过真实用量，清空就是毁数据。
    # 改成记下"现在有几条"，只看之后新增的部分。**这个习惯比代码本身值钱。**
    before, _bad = usage_log.read_all(cfg)
    n_before = len(before)
    print("日志：%s" % usage_log.log_path(cfg))
    print("开始前已有 %d 条记录（不会被清掉）" % n_before)
    print()

    mock_srv = uvicorn.Server(uvicorn.Config(mock, host="127.0.0.1", port=MOCK_PORT, log_level="warning"))
    gw_srv = uvicorn.Server(uvicorn.Config(gw.app, host=listen["host"], port=listen["port"], log_level="warning"))

    tasks = [asyncio.create_task(mock_srv.serve()), asyncio.create_task(gw_srv.serve())]
    await _wait_started(mock_srv)
    await _wait_started(gw_srv)

    print("=" * 74)
    print("第 1 部分 · 四种正常组合")
    print("=" * 74)
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as c:
            h = await c.get(base + "/healthz")
            check("网关探活 /healthz", h.status_code == 200 and h.json().get("ok"),
                  "认识 %d 个模型名" % len(h.json().get("models", [])))

            # 客户端「添加自定义模型」时会先调这个端点。缺了它，客户端报的错
            # 会是"连接失败/API Key 无效"，指向一个完全不存在的原因。
            m = await c.get(base + "/v1/models", headers={"authorization": "Bearer " + key})
            ids = [x.get("id") for x in (m.json().get("data") or [])] if m.status_code == 200 else []
            check("GET /v1/models 可用（客户端添加模型时要调）",
                  m.status_code == 200 and GROW in ids and GCLAUDE in ids,
                  "HTTP %s，列出 %d 个模型名" % (m.status_code, len(ids)))
            leaked_txt = json.dumps(m.json(), ensure_ascii=False) if m.status_code == 200 else ""
            check("模型列表只暴露对外名（不泄露上游真名/host/env 变量名）",
                  "qwen-plus" not in leaked_txt and "dashscope" not in leaked_txt
                  and "api_key_env" not in leaked_txt, "")
            m2 = await c.get(base + "/v1/models", headers={"authorization": "Bearer wrong-key"})
            check("模型列表同样校验网关密钥 → 401", m2.status_code == 401,
                  "HTTP %s" % m2.status_code)

            # 客户端若把网关当成 HTTP 代理用（本机开了「不绕过回环地址」的系统代理时
            # 就会这样），请求行会变成绝对形式。这一组验网关能不能听懂这种方言。
            async with httpx.AsyncClient(proxy=base, timeout=10.0, trust_env=False) as pc:
                ap = await pc.get(base + "/v1/models", headers={"authorization": "Bearer " + key})
                ids2 = [x.get("id") for x in (ap.json().get("data") or [])] if ap.status_code == 200 else []
                check("绝对形式请求行（网关被当成代理）仍能正确路由",
                      ap.status_code == 200 and GROW in ids2,
                      "HTTP %s（不修的话这里必是 404 Not Found）" % ap.status_code)
                ap2 = await pc.post(base + "/v1/chat/completions",
                                    headers={"authorization": "Bearer " + key},
                                    json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
                check("绝对形式下的 POST 也照常（真实流量走的就是这条）",
                      ap2.status_code == 400, "HTTP %s" % ap2.status_code)

            await _case(c, base, key, route="/v1/chat/completions", model=GROW, stream=False,
                        auth_style="bearer", label="openai 协议 · 非流式")
            await _case(c, base, key, route="/v1/chat/completions", model=GROW, stream=True,
                        auth_style="bearer", label="openai 协议 · 流式")
            await _case(c, base, key, route="/v1/messages", model=GCLAUDE, stream=False,
                        auth_style="x-api-key", label="anthropic 协议 · 非流式")
            await _case(c, base, key, route="/v1/messages", model=GCLAUDE, stream=True,
                        auth_style="x-api-key", label="anthropic 协议 · 流式")

            # 协议转换（非流式 anthropic→openai）：客户端说 Anthropic，上游是 OpenAI。
            # 用 gw-mock-openai（上游 openai）走 /v1/messages（anthropic 路由）触发转换。
            r = await c.post(base + "/v1/messages",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": GROW, "max_tokens": 256, "stream": False,
                                   "system": "你是助手",
                                   "messages": [{"role": "user", "content": [{"type": "text", "text": "你好"}]}]})
            rj = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            check("协议转换（anthropic→openai 非流式）→ 200 且是 Anthropic 格式",
                  r.status_code == 200 and rj.get("type") == "message"
                  and isinstance(rj.get("content"), list)
                  and (rj["content"][0].get("type") if rj["content"] else None) == "text",
                  "HTTP %s" % r.status_code)
            check("转换回填了客户端模型名 + 正文正确 + stop_reason 映射",
                  r.status_code == 200 and rj.get("model") == GROW
                  and (rj.get("content") or [{}])[0].get("text") == "".join(MOCK_TEXT)
                  and rj.get("stop_reason") == "end_turn", "")

            print()
            print("=" * 74)
            print("第 2 部分 · 四个「应该被拦住 / 必须能通过」的情况")
            print("=" * 74)

            # 模型名写错 —— 得明确告诉人怎么办，而不是丢一个 500
            r = await c.post(base + "/v1/chat/completions",
                             headers={"authorization": "Bearer " + key},
                             json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            check("模型名没在映射表里 → 400 且提示可用名字",
                  r.status_code == 400 and "gw-mock-openai" in json.dumps(body, ensure_ascii=False),
                  "HTTP %s" % r.status_code)

            # 反向转换（openai 客户端 → anthropic 上游）还没做 —— 必须明确拦住，不能静默透传错格式
            r = await c.post(base + "/v1/chat/completions",
                             headers={"authorization": "Bearer " + key},
                             json={"model": GCLAUDE, "messages": [{"role": "user", "content": "hi"}]})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            check("反向转换（openai→anthropic）还没做 → 400 说明方向不支持",
                  r.status_code == 400 and "方向" in json.dumps(body, ensure_ascii=False),
                  "HTTP %s" % r.status_code)

            # 流式协议转换（anthropic→openai）：客户端说 Anthropic，上游吐 OpenAI 的 SSE，
            # 网关要边收边翻成 Anthropic 的 SSE —— 这正是 Claude Code 默认走的路径。
            rstream = await c.post(base + "/v1/messages",
                                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                                   json={"model": GROW, "max_tokens": 256, "stream": True,
                                         "messages": [{"role": "user", "content": "hi"}]})
            sbody = rstream.text
            check("流式协议转换（anthropic→openai）→ 200 且是 Anthropic SSE",
                  rstream.status_code == 200
                  and "event: message_start" in sbody
                  and "event: content_block_delta" in sbody
                  and "event: message_delta" in sbody
                  and "event: message_stop" in sbody,
                  "HTTP %s，%d 字节" % (rstream.status_code, len(sbody)))
            sflat = sbody.replace("\n", "")
            smissing = [t for t in MOCK_TEXT if t not in sflat]
            check("流式转换正文完整 + stop_reason 映射为 end_turn",
                  not smissing and '"stop_reason": "end_turn"' in sbody,
                  "缺 %s" % smissing if smissing else "")

            # 工具调用转换（非流式）：带 tools 的 anthropic 请求，假上游返回 tool_calls，
            # 网关要转回 tool_use block。
            TOOL = [{"name": "read_file", "description": "读文件",
                     "input_schema": {"type": "object",
                                      "properties": {"path": {"type": "string"}}}}]
            r = await c.post(base + "/v1/messages",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": GROW, "max_tokens": 256, "stream": False,
                                   "tools": TOOL,
                                   "messages": [{"role": "user", "content": "读 main.py"}]})
            rj = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            tool_blocks = [b for b in (rj.get("content") or [])
                           if isinstance(b, dict) and b.get("type") == "tool_use"]
            check("工具调用转换（非流式）→ tool_use + stop_reason=tool_use",
                  r.status_code == 200 and len(tool_blocks) == 1
                  and tool_blocks[0].get("name") == "read_file"
                  and tool_blocks[0].get("input") == {"path": "main.py"}
                  and rj.get("stop_reason") == "tool_use", "HTTP %s" % r.status_code)

            # 工具调用转换（流式）：带 tools 的流式请求，假上游流式吐 tool_calls 分片。
            rstream = await c.post(base + "/v1/messages",
                                   headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                                   json={"model": GROW, "max_tokens": 256, "stream": True,
                                         "tools": TOOL,
                                         "messages": [{"role": "user", "content": "读 main.py"}]})
            sbody = rstream.text
            check("工具调用转换（流式）→ tool_use 事件 + input_json_delta + stop_reason=tool_use",
                  rstream.status_code == 200
                  and '"type": "tool_use"' in sbody
                  and "input_json_delta" in sbody
                  and '"stop_reason": "tool_use"' in sbody,
                  "HTTP %s" % rstream.status_code)

            # 网关 key 错
            r = await c.post(base + "/v1/chat/completions",
                             headers={"authorization": "Bearer wrong-key"},
                             json={"model": GROW, "messages": [{"role": "user", "content": "hi"}]})
            check("网关密钥不对 → 401", r.status_code == 401, "HTTP %s" % r.status_code)

            # count_tokens 这个端点也要能通过，否则 Claude Code 第一步就挂
            r = await c.post(base + "/v1/messages/count_tokens",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": GCLAUDE, "messages": [{"role": "user", "content": "hi"}]})
            check("count_tokens 端点可透传（Claude Code 第一步要用）",
                  r.status_code == 200 and r.json().get("input_tokens") == MOCK_INPUT,
                  "HTTP %s" % r.status_code)

            # -----------------------------------------------------
            # 预算闸门（集成）：临时打开预算 + 给 mock 模型配个价，验证两件事：
            #   ① 计费请求被 402 拦下（带 x-gateway-budget 头 + 明确错误类型）
            #   ② 不计费的 count_tokens 端点不受限（预算开着仍 200）
            # 注意：预算拦截发生在转发上游【之前】，所以被拦的请求不落账 ——
            #       不会干扰后面「8 条计费记录」的对账。
            # -----------------------------------------------------
            gw.CONFIG["models"]["gw-mock-openai"]["price_per_million"] = {
                "input": 1000.0, "output": 0, "cache_read": 0, "cache_write": 0,
            }
            gw.CONFIG["budget"] = {"enabled": True, "window": "total",
                                   "cap_yuan": 0.001, "preflight": False}

            r = await c.post(base + "/v1/chat/completions",
                             headers={"authorization": "Bearer " + key},
                             json={"model": GROW, "messages": [{"role": "user", "content": "hi"}]})
            rjson = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            check("预算闸门：预算用尽时计费请求 → 402 + 头 + 明确错误类型",
                  r.status_code == 402
                  and r.headers.get("x-gateway-budget") == "exceeded"
                  and (rjson.get("error") or {}).get("type") == "gateway_budget_exceeded",
                  "HTTP %s" % r.status_code)

            r = await c.post(base + "/v1/messages/count_tokens",
                             headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                             json={"model": GCLAUDE, "messages": [{"role": "user", "content": "hi"}]})
            check("预算闸门：count_tokens 不计费端点不受限（预算开着仍 200）",
                  r.status_code == 200, "HTTP %s" % r.status_code)

            # 还原：mock 模型一旦有了价，后面对账里的成本就不再是 0 了，别留下
            gw.CONFIG["models"]["gw-mock-openai"].pop("price_per_million", None)
            gw.CONFIG.pop("budget", None)
    finally:
        mock_srv.should_exit = True
        gw_srv.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0.2)

    # -------------------------------------------------------------
    # 第 3 部分 · 对账（这才是重点：日志里的数字对不对）
    # -------------------------------------------------------------
    print()
    print("=" * 74)
    print("第 3 部分 · 日志对账（假上游给的数字 vs 日志里记的）")
    print("=" * 74)

    after, bad = usage_log.read_all(cfg)
    mine = [r for r in after[n_before:] if str(r.get("virtual_model", "")).startswith("gw-mock")]
    print("本次新增 %d 条（历史 %d 条保留着）｜坏行 %d" % (len(mine), n_before, bad))
    print()

    print("%-3s %-24s %-9s %-6s %-6s %7s %7s %7s %7s  %s"
          % ("#", "模型（对外名）", "协议", "流式", "计费", "输入", "输出", "缓存读", "缓存写", "数字来源"))
    print("-" * 104)
    for i, r in enumerate(mine, 1):
        print("%-3d %-24s %-9s %-6s %-6s %7s %7s %7s %7s   %s"
              % (i, r.get("virtual_model"), r.get("protocol"),
                 "是" if r.get("stream") else "否",
                 "是" if r.get("billable") else "否",
                 r.get("input_tokens", "-"), r.get("output_tokens", "-"),
                 r.get("cache_read_tokens", "-"), r.get("cache_write_tokens", "-"),
                 "上游给的" if r.get("usage_reported") else "自己数的"))
    print()

    billable = [r for r in mine if r.get("billable")]
    non_billable = [r for r in mine if not r.get("billable")]
    streams = [r for r in billable if r.get("stream")]
    openai_stream = [r for r in streams if r.get("protocol") == "openai"]
    anth_stream = [r for r in streams if r.get("protocol") == "anthropic"]

    check("八条计费请求各记了一行（4 条透传 + 4 条转换）",
          len(billable) == 8, "实际 %d 条；另有 %d 条不计费" % (len(billable), len(non_billable)))
    check("count_tokens 那条被标成不计费（不然汇总时会被假 0 拉低）",
          len(non_billable) == 2 and all(n.get("usage_reported") is False for n in non_billable),
          "它没有 usage 对象，所以记成「没用量」而不是「用量为 0」")
    check("所有计费记录的数字都来自上游（没有退化成估算）",
          bool(billable) and all(r.get("usage_reported") for r in billable), "")
    check("输入/输出 token 与假上游给的完全一致",
          all(r.get("input_tokens") == MOCK_INPUT and r.get("output_tokens") == MOCK_OUTPUT
              for r in billable), "期望 %d / %d" % (MOCK_INPUT, MOCK_OUTPUT))
    check("缓存字段没有串位（读 %d；写：上游为 openai 应是 0，anthropic 应是 %d）"
          % (MOCK_CACHE_READ, MOCK_CACHE_WRITE),
          all(r.get("cache_read_tokens") == MOCK_CACHE_READ for r in billable)
          and all(r.get("cache_write_tokens") == (MOCK_CACHE_WRITE if r.get("upstream_protocol") == "anthropic" else 0)
                  for r in billable),
          "")
    check("OpenAI 流式确实注入了 stream_options.include_usage",
          bool(openai_stream) and openai_stream[0].get("injected_stream_options") is True,
          "没注入的话上游根本不会给 usage")
    check("Anthropic 流式不需要注入（本来就给）",
          bool(anth_stream) and anth_stream[0].get("injected_stream_options") is False, "")
    check("流式的 usage 是拼出来的而不是估算（工具调用流式无正文，字符数允许为空）",
          all(r.get("usage_reported") for r in streams)
          and all(r.get("output_chars") or (r.get("tool_count") or 0) > 0 for r in streams),
          "同时记了字符数，可以交叉验证")
    check("日志里确实没有请求原文（只记了统计量）",
          all("messages" not in r and "system" not in r for r in mine)
          and all("msg_count" in r and "prompt_chars" in r for r in mine),
          "可以安全开源")
    converted = [r for r in mine if r.get("converted")]
    check("四条转换请求都被标记 converted=true，且记账用上游协议读、数字与假上游一致",
          len(converted) == 4
          and all(r.get("upstream_protocol") == "openai" for r in converted)
          and all(r.get("input_tokens") == MOCK_INPUT
                  and r.get("output_tokens") == MOCK_OUTPUT for r in converted),
          "转换不影响记账")

    # -------------------------------------------------------------
    total = len(CHECKS)
    passed = sum(1 for _n, ok, _d in CHECKS if ok)
    print()
    print("=" * 74)
    print("结果：%d / %d 项通过" % (passed, total))
    print("=" * 74)
    for name, ok, detail in CHECKS:
        if not ok:
            print("  未通过：%s  %s" % (name, detail))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
