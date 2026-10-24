# -*- coding: utf-8 -*-
"""
e2e_long_run.py —— 真实百炼多轮 agent loop 长跑验证

模拟 Claude Code 的真实行为：走 Anthropic 协议（/v1/messages），流式接收，
模型返回 tool_use 就构造 tool_result 喂回去继续下一轮，直到 stop_reason=end_turn。
验证 anthropic→openai 转换在【真实模型 + 连续多轮 + 工具调用往返】下稳定不崩。

和 e2e_tool_call.py（单次往返）的区别：这里跑的是**连续 agent loop**，
正是 README「下一步」里说的「接 Claude Code 真实长跑」那一块。

会真实消耗 token（多轮，qwen-plus-latest 上游，免费额度内）。
"""
import asyncio
import json
import os
import time

# 1. 先把上游 key 从注册表捞进环境（必须在 import server 之前）
if not os.environ.get("DASHSCOPE_API_KEY"):
    import winreg
    k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment")
    v, _ = winreg.QueryValueEx(k, "DASHSCOPE_API_KEY")
    os.environ["DASHSCOPE_API_KEY"] = v

os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

import httpx
import uvicorn

import server as gw

# 客户端请求的模型名：config 里 protocol=openai、上游=qwen-plus-latest。
# 客户端走 anthropic 入口 → 协议不一致 → 触发 anthropic→openai 转换。
MODEL = "claude-sonnet-4-5"

SYSTEM = (
    "你是一个编程助手。要查看文件内容或执行命令时，必须调用工具，"
    "不要凭空编造文件内容。拿到工具结果后，据实回答。"
)

TOOLS = [
    {"name": "read_file", "description": "读取文件内容",
     "input_schema": {"type": "object",
                      "properties": {"path": {"type": "string", "description": "文件路径"}},
                      "required": ["path"]}},
    {"name": "run_command", "description": "执行 shell 命令",
     "input_schema": {"type": "object",
                      "properties": {"cmd": {"type": "string", "description": "要执行的命令"}},
                      "required": ["cmd"]}},
]


def simulate_tool(name, input_json_str):
    """模拟 Claude Code 执行工具，返回「工具结果」字符串。"""
    try:
        args = json.loads(input_json_str) if input_json_str else {}
    except Exception:
        args = {}
    if name == "read_file":
        path = args.get("path", "?")
        return "文件 %s 的内容：\ndef hello():\n    return 'ok'\n# 该文件没有 TODO 标记。" % path
    if name == "run_command":
        cmd = args.get("cmd", "?")
        return "$ %s\n(空输出，退出码 0)" % cmd
    return "(未知工具)"


def parse_sse(text):
    """解析 anthropic 流式 SSE，返回 (blocks, stop_reason)。

    blocks: dict index -> {type, name, id, text, input_json}
    """
    blocks = {}
    stop_reason = None
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        d = line[6:].strip()
        if d == "[DONE]":
            continue
        try:
            obj = json.loads(d)
        except Exception:
            continue
        t = obj.get("type")
        if t == "content_block_start":
            cb = obj.get("content_block") or {}
            idx = obj.get("index", 0)
            blocks[idx] = {"type": cb.get("type"), "name": cb.get("name"),
                           "id": cb.get("id"), "text": "", "input_json": ""}
        elif t == "content_block_delta":
            delta = obj.get("delta") or {}
            idx = obj.get("index", 0)
            if idx not in blocks:
                blocks[idx] = {"type": None, "name": None, "id": None,
                               "text": "", "input_json": ""}
            if delta.get("type") == "text_delta":
                blocks[idx]["text"] += delta.get("text", "")
            elif delta.get("type") == "input_json_delta":
                blocks[idx]["input_json"] += delta.get("partial_json", "")
        elif t == "message_delta":
            stop_reason = (obj.get("delta") or {}).get("stop_reason")
    return blocks, stop_reason


async def main():
    cfg = gw.CONFIG
    listen = cfg["listen"]
    base = "http://%s:%d" % (listen["host"], listen["port"])
    gw_key = (cfg.get("gateway_keys") or ["sk-gw-local-dev"])[0]

    srv = uvicorn.Server(uvicorn.Config(gw.app, host=listen["host"],
                                        port=listen["port"], log_level="warning"))
    task = asyncio.create_task(srv.serve())
    t0 = time.time()
    while not srv.started:
        if time.time() - t0 > 20:
            raise RuntimeError("网关启动超时")
        await asyncio.sleep(0.05)

    headers = {"x-api-key": gw_key, "anthropic-version": "2023-06-01"}

    # 初始用户消息：诱导模型先读文件、再下结论
    messages = [{"role": "user",
                 "content": "帮我看看 gateway/server.py 里有没有 TODO 或没做完的地方，用工具确认后回答我。"}]

    print("=" * 72)
    print("多轮 agent loop 长跑（真实百炼，anthropic→openai 转换）")
    print("=" * 72)

    ok = True
    rounds = 0
    tool_calls_total = 0
    final_text = ""

    async with httpx.AsyncClient(timeout=180.0, trust_env=False) as c:
        while rounds < 6:
            rounds += 1
            req = {"model": MODEL, "max_tokens": 512, "system": SYSTEM,
                   "tools": TOOLS, "messages": messages, "stream": True}
            r = await c.post(base + "/v1/messages", headers=headers, json=req)
            blocks, stop = parse_sse(r.text)

            print("\n第 %d 轮  HTTP %s  stop_reason=%s" % (rounds, r.status_code, stop))
            if r.status_code != 200:
                ok = False
                print("  [x] 非 200，响应:", r.text[:200])
                break

            tool_uses = []
            for idx in sorted(blocks):
                b = blocks[idx]
                if b["type"] == "text" and b["text"]:
                    print("  text[%d]: %s" % (idx, b["text"][:90]))
                elif b["type"] == "tool_use":
                    tool_uses.append(b)
                    tool_calls_total += 1
                    print("  tool_use[%d]: %s(%s)" % (idx, b["name"], b["input_json"][:70]))

            if stop == "tool_use" and tool_uses:
                # 构造 assistant 的 tool_use 块 + user 的 tool_result 块
                assistant_blocks = [{"type": "tool_use", "id": b["id"],
                                     "name": b["name"], "input": json.loads(b["input_json"] or "{}")}
                                    for b in tool_uses]
                result_blocks = [{"type": "tool_result", "tool_use_id": b["id"],
                                  "content": simulate_tool(b["name"], b["input_json"])}
                                 for b in tool_uses]
                messages.append({"role": "assistant", "content": assistant_blocks})
                messages.append({"role": "user", "content": result_blocks})
                continue
            else:
                # end_turn（或 max_tokens 截断）—— 取最后一段 text 作为最终回答
                for idx in sorted(blocks, reverse=True):
                    if blocks[idx]["type"] == "text" and blocks[idx]["text"]:
                        final_text = blocks[idx]["text"]
                        break
                break

    srv.should_exit = True
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0.2)

    print("\n" + "=" * 72)
    print("结论")
    print("=" * 72)
    print("轮数: %d | 工具调用次数: %d | 最终 stop_reason: %s" % (rounds, tool_calls_total, stop))
    print("最终回答: %s" % (final_text[:120] if final_text else "(空)"))
    passed = ok and rounds >= 2 and tool_calls_total >= 1 and final_text
    print("\n" + ("多轮 agent loop 长跑：走通" if passed else "多轮 agent loop 长跑：未走通，见上"))
    return 0 if passed else 1


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
