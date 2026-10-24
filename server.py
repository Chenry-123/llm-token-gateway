# -*- coding: utf-8 -*-
"""
server.py —— 双路由透传网关（第一步的全部范围）

它做四件事，一件都不多：

  1. 收下请求（两条路由：OpenAI 的 /v1/chat/completions、Anthropic 的 /v1/messages）
  2. 转发给上游（只动三样：模型名、认证头、加了 stream_options）
  3. 原样返回（流式就边收边转，不缓冲）
  4. 追加写一行用量日志

这一步**不做**：MCP、界面、部署。（缓存、模型路由、协议转换、预算闸门
都是后续步加进来的，各自有独立开关，见 config.json。）
理由：要先拿到"没有优化"的那条基线。没有基线，"省了多少钱"就无从谈起 ——
而且"我优化了"这句话会变成一句无法证伪的话。

启动：python -m uvicorn server:app --host 127.0.0.1 --port 8317
       （或者直接双击同目录下的 run.bat）
"""

import json
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

import budget
import convert
import protocols
import router
import usage_log
from protocols import PROTOCOL_AUTH, ROUTES, SSEDecoder, STREAM_USAGE
from upstream import UpstreamError, build_headers, build_url, classify_status, open_upstream

# ---------------------------------------------------------------
# 配置
# ---------------------------------------------------------------
CONFIG_PATH = os.environ.get("GW_CONFIG") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "config.json"
)


def load_config(path=None):
    p = path or CONFIG_PATH
    with open(p, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    # 记下配置文件所在目录，这样日志的相对路径按它解析，
    # 而不是按"你在哪个目录敲的命令"解析 —— 后者会让日志跑到意想不到的地方。
    cfg["_config_dir"] = os.path.dirname(os.path.abspath(p))
    return cfg


CONFIG = load_config()


class _AbsoluteFormFixer:
    """容忍「绝对形式」的请求行：`GET http://127.0.0.1:8317/v1/models HTTP/1.1`

    正常客户端发的是相对形式（`GET /v1/models`）。但只要客户端把网关**当成了
    HTTP 代理**，请求行就会变成绝对形式 —— FastAPI 会把整串
    `http://127.0.0.1:8317/v1/models` 当成路径去匹配，
    于是**每一个请求都返回 404 {"detail":"Not Found"}**。

    这个报错完全不指向真正的原因，排查代价极高。真实触发场景（本机已经踩过一次）：
        系统/环境里设了 HTTP_PROXY，且代理**没有绕过 127.0.0.1** ——
        于是本机访问网关的请求被塞进代理，代理再以绝对形式转发回网关。

    所以在这里、在路由之前把多余的部分剥掉：网关多懂一种"方言"，
    就少一类查不出来的故障。用**纯 ASGI 中间件**而不是 BaseHTTPMiddleware，
    因为后者会在流式路径上做缓冲 —— 而这条链路的命门正是 SSE 不能被缓冲。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            path = scope.get("path") or ""
            if path.startswith(("http://", "https://")):
                parts = urlsplit(path)
                scope = dict(scope)
                scope["path"] = parts.path or "/"
                scope["raw_path"] = (parts.path or "/").encode("utf-8")
                if parts.query:
                    scope["query_string"] = parts.query.encode("utf-8")
        await self.app(scope, receive, send)


app = FastAPI(
    title="LLM token gateway · step 1 (passthrough + accounting)",
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(_AbsoluteFormFixer)

# 上游是流式的，整体超时交给上游决定；只对"连上"这一步设 20 秒上限。
_client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=20.0))


# ---------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------
def _check_gateway_key(request):
    """校验客户端带的是不是我们自己的网关 key。

    返回 (通过吗, 用的是哪个头)。
    第二个值会写进日志 —— 它的用处是：**分清这次请求来自哪个客户端**。
    Qoder 走 Authorization，Claude Code 走 x-api-key。
    以后日志里出现"某类客户端特别费钱"，你就有据可查。
    """
    keys = [k for k in (CONFIG.get("gateway_keys") or []) if k]
    if not keys:
        # 没配 key = 本地裸跑模式，方便第一次验证
        return True, "none-configured"

    h = request.headers
    authz = h.get("authorization") or ""
    if authz.lower().startswith("bearer "):
        return (authz.split(" ", 1)[1].strip() in keys), "authorization"

    tok = h.get("x-api-key")
    if tok:
        return (tok in keys), "x-api-key"

    return False, "missing"


def _err(message, etype, status):
    return JSONResponse({"error": {"message": message, "type": etype}}, status_code=status)


def _budget_err(message):
    """预算用尽时的错误响应。

    单独拎出来、并加一个 x-gateway-budget 头，是为了让客户端/脚本能一眼认出
    「这是被预算闸门拦的，不是上游坏了」—— 免得用户看到 402 误以为 key 失效
    或额度清零，去排查一个根本不存在的问题。
    """
    return JSONResponse(
        {"error": {"message": message, "type": "gateway_budget_exceeded"}},
        status_code=402,
        headers={"x-gateway-budget": "exceeded"},
    )


def _pick_response_headers(resp):
    """只挑几个该带回去的头。

    特别注意：**不带 content-encoding**。
    因为下游（httpx）已经帮我们把 gzip 解开了，再把
    content-encoding: gzip 带回去，客户端会尝试解压一段已经不是 gzip 的数据 ——
    报的错会非常难懂（通常是一句莫名其妙的 decode error）。
    """
    out = {}
    for h in ("cache-control", "x-request-id", "request-id"):
        v = resp.headers.get(h)
        if v:
            out[h] = v
    return out


# ---------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------
async def _handle(request, route):
    t0 = time.perf_counter()
    route_info = ROUTES[route]
    protocol = route_info["protocol"]
    request_id = uuid.uuid4().hex[:12]

    passed, client_hint = _check_gateway_key(request)
    if not passed:
        return _err(
            "网关密钥不对。客户端里填的 API Key 应该等于 config.json 里的 gateway_keys，"
            "而不是上游厂商的 key。",
            "gateway_auth",
            401,
        )

    raw = await request.body()
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError as e:
        return _err("请求体不是合法 JSON：%s" % e, "gateway_bad_request", 400)

    # ---- 找模型 ----
    virtual_model = body.get("model")
    models = CONFIG.get("models") or {}
    if virtual_model not in models:
        return _err(
            "网关不认识模型名 '%s'。config.json 里已经配好的是：%s"
            % (virtual_model, ", ".join(sorted(models))),
            "gateway_unknown_model",
            400,
        )
    mc = models[virtual_model]

    # ---- 协议：客户端（路由）vs 上游（配置）----
    # 一致 → 纯透传；不一致 → 走 convert.py 做翻译。
    # 目前只打通「anthropic→openai」这一个方向（Claude Code → 百炼），
    # 流式和非流式都支持。其余方向先明确报错 —— 宁可报错，也不能静默
    # 透传错格式：那样客户端会收到它看不懂的响应，比报错难查得多。
    client_protocol = protocol
    upstream_protocol = mc.get("protocol") or ""
    need_convert = client_protocol != upstream_protocol
    is_stream = bool(body.get("stream"))

    if need_convert:
        if not route_info.get("counts_usage", True):
            # count_tokens 这类端点没有 OpenAI 对应物，转换没有意义，先拦下
            return _err(
                "端点 %s 暂不支持协议转换。" % route,
                "gateway_convert_endpoint_unsupported",
                400,
            )
        if not (client_protocol == "anthropic" and upstream_protocol == "openai"):
            return _err(
                "协议转换目前只支持 anthropic → openai 方向，你这条是 %s → %s。"
                % (client_protocol, upstream_protocol),
                "gateway_convert_direction_unsupported",
                400,
            )

    # ---- 上游 key：从环境变量里取，不从配置里取 ----
    key_env = mc.get("api_key_env")
    upstream_key = os.environ.get(key_env or "", "")
    if not upstream_key:
        # 自验用的假上游是本机进程，不需要真 key
        if not (mc.get("base_url") or "").startswith("http://127.0.0.1"):
            return _err(
                "环境变量 %s 没有设置。先在 PowerShell 里设置它再启动网关："
                "$env:%s=\"你的key\"" % (key_env, key_env),
                "gateway_missing_key",
                500,
            )
        upstream_key = "mock-key"

    # ---- 模型路由（决策层）：easy 走便宜模型，medium/hard 走旗舰 ----
    # 必须在「改写请求体」之前 —— 路由要看原始 messages 猜难度，
    # 而 convert 会重排 body 结构，晚了就猜不到了。
    upstream_model, billed_model, routed, difficulty = router.pick_upstream(
        mc, models, body
    )

    # ---- 改写请求体（全项目只有这几行会动请求体）----
    if need_convert:
        # 整段翻译（含 system 位置、content 格式、model 替换），不再逐字段手改
        body = convert.anthropic_request_to_openai(body, upstream_model)
    else:
        body["model"] = upstream_model      # 把对外名字换成上游真名
    injected = False
    if is_stream:
        injected = protocols.inject_stream_usage(
            upstream_protocol, body, (CONFIG.get("stream") or {}).get("inject_include_usage", True)
        )

    # ---- 显式缓存（无损优化）：给 system 前缀打 cache_control 标记 ----
    # 必须在转换**之后**打 —— 转换后 body 才长成「上游协议」的形状，
    # 这里按 upstream_protocol 打，正好打在它最终要发出去的样子上。
    cache_injected = protocols.inject_explicit_cache(
        upstream_protocol, body, (CONFIG.get("cache") or {}).get("explicit", False)
    )

    # ！ 转换时，上游路径和认证头都得按【上游协议】取，不能沿用路由的：
    #    客户端走 /v1/messages（anthropic），但上游百炼要 /chat/completions + Bearer 头。
    upstream_path = route_info["upstream_path"]
    if need_convert:
        upstream_path = protocols.PROTOCOL_CHAT_PATH.get(upstream_protocol, upstream_path)
    new_raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    url = build_url(mc, upstream_path)
    headers = build_headers(mc, upstream_protocol, request.headers, upstream_key, PROTOCOL_AUTH.get(upstream_protocol))
    stats = protocols.request_stats(upstream_protocol, body)
    billable = bool(route_info.get("counts_usage", True))

    # ---- 预算闸门：在「把钱花出去」之前拦住 ----
    # 放在路由/改体/统计之后、open_upstream 之前：预检需要 billed_model（按谁计价）
    # 和输入字符数，硬上限需要「已经花了多少」—— 这些此时都齐了。
    # count_tokens 这类不计费端点（billable=False）不受预算约束。
    if billable:
        budget_reject = budget.check(
            CONFIG,
            billed_model=billed_model or virtual_model,
            total_input_chars=stats["prompt_chars"] + stats["system_chars"],
        )
        if budget_reject:
            return _budget_err(budget_reject[1])

    base_record = {
        "request_id": request_id,
        "route": route,
        "protocol": client_protocol,
        "upstream_protocol": upstream_protocol,
        "converted": need_convert,
        "client_hint": client_hint,
        "virtual_model": virtual_model,
        "upstream_model": upstream_model,
        # 路由相关：billed_model 是「成本按谁的单价算」。路由把 easy 请求换成了
        # 便宜模型后，virtual_model 仍是客户端要的 gw-qwen，但钱得按 turbo 算。
        # 没路由时 billed_model == virtual_model，行为与旧版完全一致。
        "billed_model": billed_model or virtual_model,
        "routed": routed,
        "difficulty": difficulty,
        "upstream_host": httpx.URL(url).host,
        "stream": is_stream,
        "injected_stream_options": injected,
        "explicit_cache": cache_injected,
        # 这条请不清算进成本。像 count_tokens 这种端点也会打日志，
        # 但它不产生费用 —— 不标出来，汇总时就会莫名多出一堆 0 拉低均值。
        "billable": billable,
        # 下面是"请求统计量"——只有形状，没有内容
        "msg_count": stats["msg_count"],
        "prompt_chars": stats["prompt_chars"],
        "system_chars": stats["system_chars"],
        "tool_count": stats["tool_count"],
        "has_image": stats["has_image"],
    }

    def _ms():
        return int((time.perf_counter() - t0) * 1000)

    # ---- 发给上游 ----
    try:
        resp, retries = await open_upstream(
            _client, url, headers, new_raw, CONFIG.get("retry") or {}
        )
    except UpstreamError as e:
        usage_log.append(CONFIG, dict(
            base_record, ok=False, status=None, retry_count=0,
            latency_ms=_ms(), error_kind=e.kind, detail=e.detail[:300],
        ))
        return _err(
            "连不上上游（%s）：%s" % (e.kind, e.detail),
            "gateway_upstream_unreachable",
            502,
        )

    # ---- 上游报错：原样转发它的错误，并记一笔 ----
    if resp.status_code >= 400:
        raw_resp = await resp.aread()
        await resp.aclose()
        kind = classify_status(resp.status_code)
        usage_log.append(CONFIG, dict(
            base_record, ok=False, status=resp.status_code, retry_count=retries,
            latency_ms=_ms(), error_kind=kind,
        ))
        return Response(
            content=raw_resp,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    # ---- 非流式：读全，取用量，记一笔，原样返回 ----
    if not is_stream:
        raw_resp = await resp.aread()
        await resp.aclose()
        usage = {}
        try:
            # 记账读的是【上游原始响应】，所以用上游协议解字段（转换过的响应字段名会变）
            usage = protocols.usage_from_json(upstream_protocol, json.loads(raw_resp))
        except Exception:                    # noqa: BLE001
            usage = {}
        # 非流式也标上这个来源标记 —— 后面做统计时，
        # "上游给的数字"和"我们自己数的数字"绝对不能混在一起算
        reported = bool(usage)
        usage["usage_reported"] = reported
        usage_log.append(CONFIG, dict(
            base_record, ok=True, status=resp.status_code, retry_count=retries,
            latency_ms=_ms(), error_kind=None, **usage,
        ))
        # 协议不一致时，把上游响应翻译回客户端协议再返回
        if need_convert:
            try:
                raw_resp = json.dumps(
                    convert.openai_response_to_anthropic(
                        json.loads(raw_resp), virtual_model
                    ),
                    ensure_ascii=False,
                ).encode("utf-8")
            except Exception:                # noqa: BLE001
                pass                           # 翻译失败就把上游原文还回去，绝不吞掉响应
        return Response(
            content=raw_resp,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type", "application/json"),
        )

    # ---- 流式：边收边转，同时旁路捡用量 ----
    usage = STREAM_USAGE[upstream_protocol](upstream_protocol)
    decoder = SSEDecoder()
    # 协议不一致时，边收边把 OpenAI 的流翻译成 Anthropic 的流。
    # 输入量在流式下拿不到真值（OpenAI 把 usage 放在最后一块），
    # 先按请求字符数估一个 —— 只喂给 Claude Code 做展示，记账不靠它。
    translator = None
    if need_convert:
        translator = convert.OpenAIStreamToAnthropic(
            virtual_model,
            estimated_input_tokens=max(1, (stats["prompt_chars"] + stats["system_chars"]) // 4),
        )

    async def _relay():
        """注意这里的三件事：
             1. 每收到一块**立刻 yield 出去** —— 不攒批。攒了就没有流式体验了。
             2. 同一块同时喂给解码器。转发和解析是**两条独立的路**，
                解析出错绝不允许影响转发（所以有那个 try/except）。
             3. 不管怎么结束（正常、异常、客户端断线），都要把账记上。
                这就是为什么记账写在 finally 里 —— **跑掉的账比多花的钱更可怕**。

           协议转换时（need_convert），第 1 条改成「翻译后再 yield」——
           上游吐的是 OpenAI 的 SSE，得翻成 Anthropic 的 SSE 再给 Claude Code。
           记账用的 usage 累加器**始终**吃上游原始载荷，与翻译无关。
        """
        finished = False
        try:
            async for chunk in resp.aiter_bytes():
                datas = decoder.feed(chunk)
                if need_convert:
                    for data in datas:
                        if data.strip() == "[DONE]":
                            for piece in translator.finish():
                                yield piece
                            continue
                        try:
                            payload = json.loads(data)
                        except Exception:        # noqa: BLE001
                            continue
                        usage.feed(payload)
                        for piece in translator.feed(payload):
                            yield piece
                else:
                    yield chunk
                    for data in datas:
                        if data.strip() == "[DONE]":
                            continue
                        try:
                            usage.feed(json.loads(data))
                        except Exception:        # noqa: BLE001
                            pass                     # 解析失败绝不影响转发
            # 上游若没发 [DONE] 就断流，这里兜底补上收尾事件（幂等，不会重复）
            if need_convert:
                for piece in translator.finish():
                    yield piece
            finished = True
        finally:
            await resp.aclose()
            u = usage.as_dict()
            usage_log.append(CONFIG, dict(
                base_record,
                ok=finished,
                status=resp.status_code,
                retry_count=retries,
                latency_ms=_ms(),
                error_kind=None if finished else "client_disconnect",
                input_tokens=u["input_tokens"],
                output_tokens=u["output_tokens"],
                cache_read_tokens=u["cache_read_tokens"],
                cache_write_tokens=u["cache_write_tokens"],
                # 这个布尔值比 token 数字本身还重要：它标出数字的来源。
                usage_reported=u["usage_reported"],
                output_chars=u["output_chars"],
                stop_reason=u["stop_reason"],
            ))

    return StreamingResponse(
        _relay(),
        status_code=resp.status_code,
        media_type="text/event-stream",
        headers=_pick_response_headers(resp),
    )


# ---------------------------------------------------------------
# 路由注册：Routes 表里有几条，就自动注册几条
#   加一个新协议 = 往 protocols.ROUTES 里加一行，这里不用动
# ---------------------------------------------------------------
def _make_handler(route):
    async def handler(request: Request):
        return await _handle(request, route)
    return handler


for _route in ROUTES:
    app.add_api_route(_route, _make_handler(_route), methods=["POST"])


@app.get("/v1/models")
async def list_models(request: Request):
    """OpenAI 兼容的模型列表端点。

    为什么要主动提供它 —— 纯粹是「堵掉一类极难查的错」：
        客户端在**保存自定义模型**或刷新模型列表时，会先 GET /v1/models。
        没有这个端点，它拿到 404，而它弹给用户的提示通常是
        「连接失败」或「API Key 无效」—— 跟真正的原因（少了个端点）
        毫无关系。报错指错方向的坑，排查代价最高，十行代码堵掉很划算。

    只列 config.json 里的**对外名**（gw-*），不列上游真名、更不列 key 的
    环境变量名 —— 「藏起上游是谁」正是网关存在的意义之一。
    """
    passed, _hint = _check_gateway_key(request)
    if not passed:
        return _err("网关密钥不对。", "gateway_auth", 401)

    created = int(time.time())
    return {
        "object": "list",
        "data": [
            {"id": name, "object": "model", "created": created, "owned_by": "token-gateway"}
            for name in sorted((CONFIG.get("models") or {}).keys())
        ],
    }


@app.get("/healthz")
async def healthz():
    """给客户端和自验脚本用的探活。也顺便告诉你网关认识哪些模型名。"""
    return {
        "ok": True,
        "routes": sorted(ROUTES.keys()),
        "models": sorted((CONFIG.get("models") or {}).keys()),
        "log": usage_log.log_path(CONFIG),
    }
