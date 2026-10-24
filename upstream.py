# -*- coding: utf-8 -*-
"""
upstream.py —— 跟上游厂商说话的那一层

它只干三件事：

  1. 把"网关收到的请求"组装成"发给上游的请求"（换地址、换 key、补必须带的头）
  2. 发出去，并且**保留流式能力** —— 拿到一块就能往回吐一块
  3. 把失败分类，而不是把异常原样扔出去

为什么单独一层、不写在 server.py 里：
    因为将来要接的不止一家。隔开之后，"加一家上游"和"改主流程"
    变成两件互不影响的事。
    这和 llm.py 那一层是同一个理由：**把会变的和不变的隔开。**
"""

import asyncio

import httpx


class UpstreamError(Exception):
    """连不上上游，或者上游在返回任何内容之前就挂了。"""

    def __init__(self, kind, detail):
        super().__init__("%s: %s" % (kind, detail))
        self.kind = kind
        self.detail = detail


# 失败分类。分这么细不是为了好看 ——
# 是为了后面能做统计："这个月失败里有多少是限流、多少是 key 过期"，
# 以及"哪些失败重试有用、哪些重试纯属浪费钱"。
_STATUS_KIND = {
    400: "bad_request",
    401: "auth",
    403: "auth",
    404: "model_or_path",
    408: "timeout",
    413: "too_large",
    422: "bad_request",
    429: "rate_limit",
}

# 这些值得重试。注意 bad_request / auth / model_or_path 不在里面 ——
# 参数错了或者 key 错了，重试一百次结果一样，只是白等。
RETRYABLE = {"timeout", "connect", "network", "upstream_server", "rate_limit"}


def classify_status(status):
    if status in _STATUS_KIND:
        return _STATUS_KIND[status]
    if 500 <= status < 600:
        return "upstream_server"
    return "http_error"


def classify_exception(exc):
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    return "unknown"


def build_url(model_cfg, upstream_path):
    base = (model_cfg.get("base_url") or "").rstrip("/")
    return base + upstream_path


def build_headers(model_cfg, protocol, incoming, upstream_key, protocol_auth):
    """组装发给上游的请求头。

    三条规则：
      1. **认证头永远换掉** —— 客户端手里那把是"网关 key"，绝不能漏给上游。
      2. **白名单头原样透传** —— Anthropic 官方点名要求的那几个。
      3. **其余客户端头一概丢掉** —— 包括 accept-language、peer 之类的无关头。
         理由：多带一个头就多一个上游可能不认的地方，而它对我们毫无价值。
    """
    auth = protocol_auth or {}
    headers = {}

    # content-type 本来就要发 JSON，所以按客户端的来，缺省 application/json
    headers["content-type"] = incoming.get("content-type") or "application/json"

    for h in (auth.get("pass_headers") or []):
        if h == "content-type":
            continue
        v = incoming.get(h)
        if v:
            headers[h] = v

    header_name = auth.get("header", "authorization")
    headers[header_name] = (auth.get("format") or "{key}").format(key=upstream_key)

    if incoming.get("accept"):
        headers["accept"] = incoming["accept"]

    return headers


async def open_upstream(client, url, headers, body_bytes, retry_cfg):
    """发请求，**保留流**。返回 (response, 重试了几次)。

    重试只发生在"上游一个字节都还没返回"之前：

      - 连接失败 / 超时 / 5xx / 429  → 可以重试
      - 一旦开始吐内容（200 + 流已开）→ 不再重试

    最后这条很重要：流式响应已经往客户端发了半截，这时候重试，
    客户端会收到"两遍开头"，生成的内容就错乱了。
    **宁可失败，也不要给出一个看起来正常但其实是拼接错的答案。**
    """
    attempts = max(1, int((retry_cfg or {}).get("max_attempts", 1)))
    backoff = float((retry_cfg or {}).get("backoff_seconds", 0.8))
    last_kind = None

    for i in range(attempts):
        try:
            req = client.build_request("POST", url, headers=headers, content=body_bytes)
            resp = await client.send(req, stream=True)
        except Exception as e:                       # noqa: BLE001
            last_kind = classify_exception(e)
            if last_kind in RETRYABLE and i < attempts - 1:
                await asyncio.sleep(backoff * (2 ** i))   # 指数退避
                continue
            raise UpstreamError(last_kind, str(e))

        status = resp.status_code
        kind = classify_status(status) if status >= 400 else None

        if kind in RETRYABLE and i < attempts - 1:
            await resp.aclose()
            await asyncio.sleep(backoff * (2 ** i))
            last_kind = kind
            continue

        # 4xx（非限流）和 200 都从这里原样返回，交给 server 决定怎么转发
        return resp, i

    raise UpstreamError(last_kind or "unknown", "重试次数用尽")
