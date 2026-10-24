# -*- coding: utf-8 -*-
"""
pricing.py —— 成本计算（把 token 用量换成钱）

这是「降本」这个词成立的地基：没有钱数，「省了多少」就是一句空话。

和 finagent/src/pricing.py 的区别（为什么没直接搬，而是重写）：
    1. 字段名：gateway 日志记的是 virtual_model（对外名），不是 model。
    2. 【关键新增】缓存 token 单独计价：cache_read / cache_write 的单价
       远低于正常输入（命中约 0.1 倍、显式创建约 1.25 倍）。不拆开算会
       高估成本 —— 而「缓存省了多少」正是这个项目要证明的核心。
    3. 单价从 config.json 的 price_per_million 读，不写死在代码里。

设计原则（和 finagent 一致，守住它）：
    日志只记「事实」（谁、用了多少 token），不记「派生值」（多少钱）。
    因为单价会变。今天 0.8 元/百万，明年可能 0.3。
    钱在【读取日志、做报告】时才算 —— 这样换了单价还能重算历史数据。

单价单位：元 / 百万 token。config 里每个模型的 price_per_million 配四项：
    input / output / cache_read / cache_write
四项都配了才计费；缺某一项就按 0 算（宁可算 0 也别用错的倍率去猜）。
"""


def _price(cfg, model):
    """取某个模型（对外名）的单价表。没配就返回空 dict。"""
    spec = (cfg.get("models") or {}).get(model) or {}
    return spec.get("price_per_million") or {}


def _cache_keys(record):
    """返回这次请求该用哪两个缓存价字段名。

    缓存有两种模式，价不同：
        隐式（自动）：命中 = 输入价 20%、创建 = 输入价 100%
        显式（打 cache_control）：命中 = 输入价 10%、创建 = 输入价 125%
    日志里的 explicit_cache 字段记着这次走了哪种模式（事实），
    这里据此选价。旧日志没有这个字段 → 按隐式算，向前兼容。
    """
    if record.get("explicit_cache"):
        return "cache_read_explicit", "cache_write_explicit"
    return "cache_read", "cache_write"


def _bill_model(record):
    """成本该按哪个模型算。

    路由会把 easy 请求的实际模型换成便宜档（记在 billed_model），
    但客户端请求的模型名（virtual_model）不变。钱必须按「实际用的」算，
    否则路由省了钱、账上还按原价记，等于白省。
    旧日志没有 billed_model 字段 → 退回 virtual_model，向前兼容。
    """
    return record.get("billed_model") or record.get("virtual_model")


def cost_of(record, cfg):
    """把一条网关日志记录换算成人民币（元）。

    record 里只有 token 数（input/output/cache_read/cache_write），没有钱。
    钱在这里算。

    【关键】input_tokens 是「总输入」，**已经包含** cache_read 和 cache_write
    这两部分（上游的 prompt_tokens 就是全量输入）。所以要先把缓存部分减掉，
    剩下的才是按正常输入价计费的部分 —— 否则缓存 token 会被算两次：
    一次按输入价、一次按缓存价，成本虚高。
    """
    p = _price(cfg, _bill_model(record))
    if not p:
        return 0.0

    def per_million(key):
        return float(p.get(key) or 0) / 1_000_000

    total_input = record.get("input_tokens") or 0
    cache_read = record.get("cache_read_tokens") or 0
    cache_write = record.get("cache_write_tokens") or 0
    uncached_input = total_input - cache_read - cache_write
    if uncached_input < 0:
        uncached_input = 0

    read_key, write_key = _cache_keys(record)
    return (
        uncached_input * per_million("input")
        + (record.get("output_tokens") or 0) * per_million("output")
        + cache_read * per_million(read_key)
        + cache_write * per_million(write_key)
    )


def total_cost(records, cfg):
    """一批记录的总成本（元）。"""
    return sum(cost_of(r, cfg) for r in records)


def group_by(records, cfg):
    """按虚拟模型分组，返回 {模型名: (条数, 成本)}。

    模型路由是这个项目的灵魂，成本按「哪个模型烧的」拆开，是第一眼该看的。
    """
    groups = {}
    for r in records:
        name = r.get("virtual_model") or "(未知)"
        groups.setdefault(name, []).append(r)
    return {
        name: (len(rs), total_cost(rs, cfg))
        for name, rs in sorted(groups.items())
    }


def cache_savings(records, cfg):
    """缓存给这批记录省了多少钱（元）。

    逻辑：
        缓存读（命中）本应按输入价算，实际按便宜的缓存价算 → 省了。
        缓存写（创建）比输入价贵（约 1.25 倍）→ 多花了，计负数。
    返回值：净节省（正数=省了，负数=反而多花）。

    为什么单算这一项：它是「缓存省了多少」的答案，而「省」正是这个项目的
    第一价值。没有这一项，缓存带来的好处会淹没在总成本里看不见。
    """
    saved = 0.0
    for r in records:
        p = _price(cfg, _bill_model(r))
        if not p:
            continue
        input_p = float(p.get("input") or 0) / 1_000_000
        read_key, write_key = _cache_keys(r)
        read_p = float(p.get(read_key) or 0) / 1_000_000
        write_p = float(p.get(write_key) or 0) / 1_000_000

        saved += (r.get("cache_read_tokens") or 0) * (input_p - read_p)
        saved += (r.get("cache_write_tokens") or 0) * (input_p - write_p)
    return saved
