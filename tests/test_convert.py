# -*- coding: utf-8 -*-
"""convert.py 的单元测试 —— 锁住协议转换（Anthropic ⇄ OpenAI）的边界。

覆盖三个方向：
  1. 请求 anthropic→openai（含 system 位置、工具调用、tool_result、图片）
  2. 响应 openai→anthropic（含 stop_reason、usage、tool_calls）
  3. 流式 OpenAI SSE → Anthropic SSE
"""

import json

import pytest

import convert


# ---------------------------------------------------------------
# 方向 1 · 请求：Anthropic → OpenAI
# ---------------------------------------------------------------

def test_content_to_text_str():
    assert convert._content_to_text("你好") == "你好"


def test_content_to_text_blocks_only_text():
    content = [
        {"type": "text", "text": "a"},
        {"type": "text", "text": "b"},
        {"type": "image", "source": {}},  # 非 text block 跳过
    ]
    assert convert._content_to_text(content) == "ab"


def test_content_to_text_non_str_non_list():
    assert convert._content_to_text(123) == ""
    assert convert._content_to_text(None) == ""


def test_image_block_to_openai():
    block = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "ABC"}}
    assert convert._anthropic_image_block_to_openai(block) == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,ABC"},
    }


def test_content_to_openai_str_passthrough():
    assert convert._content_to_openai("你好") == "你好"


def test_content_to_openai_text_only_joins():
    content = [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]
    assert convert._content_to_openai(content) == "ab"


def test_content_to_openai_text_and_image_parts():
    content = [
        {"type": "text", "text": "看图"},
        {"type": "image", "source": {"media_type": "image/jpeg", "data": "XYZ"}},
    ]
    parts = convert._content_to_openai(content)
    assert parts == [
        {"type": "text", "text": "看图"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,XYZ"}},
    ]


def test_content_to_openai_image_only():
    content = [{"type": "image", "source": {"media_type": "image/png", "data": "X"}}]
    parts = convert._content_to_openai(content)
    assert parts == [{"type": "image_url", "image_url": {"url": "data:image/png;base64,X"}}]


def test_tools_to_openai_none_when_empty():
    assert convert._anthropic_tools_to_openai(None) is None
    assert convert._anthropic_tools_to_openai([]) is None


def test_tools_to_openai_single():
    tools = [{"name": "get_weather", "description": "查天气", "input_schema": {"type": "object"}}]
    out = convert._anthropic_tools_to_openai(tools)
    assert out[0] == {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查天气",
            "parameters": {"type": "object"},
        },
    }


def test_tools_to_openai_default_schema_when_missing():
    tools = [{"name": "f"}]
    out = convert._anthropic_tools_to_openai(tools)
    assert out[0]["function"]["parameters"] == {"type": "object", "properties": {}}
    assert out[0]["function"]["description"] == ""


@pytest.mark.parametrize("tc,expected", [
    ({"type": "auto"}, "auto"),
    ({"type": "any"}, "required"),
    ({"type": "tool", "name": "f"}, {"type": "function", "function": {"name": "f"}}),
    ({"type": "none"}, "none"),
    ({"type": "weird"}, None),
])
def test_tool_choice_mapping(tc, expected):
    assert convert._anthropic_tool_choice_to_openai(tc) == expected


def test_tool_choice_non_dict_none():
    assert convert._anthropic_tool_choice_to_openai("auto") is None


def test_request_replaces_model():
    out = convert.anthropic_request_to_openai({"model": "claude-sonnet-4-5", "messages": []}, "qwen-plus")
    assert out["model"] == "qwen-plus"


def test_request_system_becomes_first_message():
    body = {
        "model": "c",
        "system": [{"type": "text", "text": "你是助手"}],
        "messages": [{"role": "user", "content": "hi"}],
    }
    out = convert.anthropic_request_to_openai(body, "qwen")
    assert out["messages"][0] == {"role": "system", "content": "你是助手"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_request_does_not_mutate_input():
    import copy
    body = {
        "model": "a",
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "hi"},
                                         {"type": "tool_use", "id": "x", "name": "f", "input": {"k": "v"}}]},
        ],
    }
    orig = copy.deepcopy(body)
    convert.anthropic_request_to_openai(body, "qwen")
    assert body == orig


def test_request_assistant_tool_use_to_tool_calls():
    body = {
        "messages": [
            {"role": "user", "content": "北京天气"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "我来查"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "北京"}},
            ]},
        ],
    }
    out = convert.anthropic_request_to_openai(body, "qwen")
    assistant = out["messages"][1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "我来查"
    assert assistant["tool_calls"][0]["id"] == "toolu_1"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"
    # arguments 是 JSON 字符串，中文不转义（ensure_ascii=False）
    assert json.loads(assistant["tool_calls"][0]["function"]["arguments"]) == {"city": "北京"}


def test_request_user_tool_result_to_tool_role():
    body = {
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": "晴，25度"},
            ]},
        ],
    }
    out = convert.anthropic_request_to_openai(body, "qwen")
    assert len(out["messages"]) == 1
    assert out["messages"][0] == {"role": "tool", "tool_call_id": "toolu_1", "content": "晴，25度"}


def test_request_stop_sequences_to_stop():
    body = {"messages": [], "stop_sequences": ["\n\nHuman:"]}
    out = convert.anthropic_request_to_openai(body, "qwen")
    assert out["stop"] == ["\n\nHuman:"]


def test_request_common_params_passthrough():
    body = {"messages": [], "temperature": 0.7, "top_p": 0.9, "stream": True, "max_tokens": 100}
    out = convert.anthropic_request_to_openai(body, "qwen")
    assert out["temperature"] == 0.7
    assert out["top_p"] == 0.9
    assert out["stream"] is True
    assert out["max_tokens"] == 100


# ---------------------------------------------------------------
# 方向 2 · 响应：OpenAI → Anthropic
# ---------------------------------------------------------------

@pytest.mark.parametrize("fr,expected", [
    ("stop", "end_turn"),
    ("length", "max_tokens"),
    ("content_filter", "refusal"),
    ("tool_calls", "tool_use"),
    (None, "end_turn"),
    ("unknown_thing", "end_turn"),
])
def test_stop_reason_mapping(fr, expected):
    assert convert._stop_reason(fr) == expected


def test_response_content_to_text_block():
    body = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    out = convert.openai_response_to_anthropic(body, "claude-sonnet-4-5")
    assert out["content"] == [{"type": "text", "text": "hi"}]
    assert out["stop_reason"] == "end_turn"
    assert out["model"] == "claude-sonnet-4-5"


def test_response_tool_calls_to_tool_use():
    body = {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "function": {"name": "f", "arguments": '{"a": 1}'}},
                ],
            },
            "finish_reason": "tool_calls",
        }],
    }
    out = convert.openai_response_to_anthropic(body, "claude")
    assert out["stop_reason"] == "tool_use"
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["id"] == "call_1"
    assert block["name"] == "f"
    assert block["input"] == {"a": 1}


def test_response_bad_arguments_falls_back_empty():
    body = {
        "choices": [{
            "message": {"tool_calls": [{"id": "c1", "function": {"name": "f", "arguments": "not-json"}}]},
            "finish_reason": "tool_calls",
        }],
    }
    out = convert.openai_response_to_anthropic(body, "claude")
    assert out["content"][0]["input"] == {}


def test_response_usage_mapping():
    body = {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 123, "completion_tokens": 45},
    }
    out = convert.openai_response_to_anthropic(body, "claude-sonnet-4-5")
    assert out["usage"]["input_tokens"] == 123
    assert out["usage"]["output_tokens"] == 45


# ---------------------------------------------------------------
# 方向 3 · 流式：OpenAI SSE → Anthropic SSE
# ---------------------------------------------------------------

def _parse_events(out):
    """把 feed/finish 产出的 bytes 列表，解析成 [(event_name, payload_dict)]。"""
    events = []
    for raw in out:
        name, data = None, None
        for line in raw.decode("utf-8").strip().split("\n"):
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        events.append((name, data))
    return events


def test_stream_text_feed_and_finish():
    tr = convert.OpenAIStreamToAnthropic("claude-sonnet-4-5", estimated_input_tokens=40)
    out = tr.feed({"choices": [{"delta": {"content": "你好"}}]})
    events = _parse_events(out)
    assert [e[0] for e in events] == ["message_start", "content_block_start", "content_block_delta"]
    assert events[0][1]["message"]["usage"]["input_tokens"] == 40
    assert events[2][1]["delta"]["text"] == "你好"

    tail = _parse_events(tr.finish())
    assert [e[0] for e in tail] == ["content_block_stop", "message_delta", "message_stop"]
    assert tail[1][1]["delta"]["stop_reason"] == "end_turn"


def test_stream_finish_idempotent():
    tr = convert.OpenAIStreamToAnthropic("claude")
    tr.finish()
    assert tr.finish() == []


def test_stream_tool_call_closes_text_then_opens_tool():
    tr = convert.OpenAIStreamToAnthropic("claude")
    tr.feed({"choices": [{"delta": {"content": "我要查"}}]})
    out = tr.feed({"choices": [{"delta": {"tool_calls": [
        {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": '{"city":'}},
    ]}}]})
    events = _parse_events(out)
    # text 先关，再开 tool_use，再发 input_json_delta
    assert [e[0] for e in events] == ["content_block_stop", "content_block_start", "content_block_delta"]
    assert events[1][1]["content_block"]["type"] == "tool_use"
    assert events[1][1]["content_block"]["name"] == "get_weather"
    assert events[2][1]["delta"]["type"] == "input_json_delta"


def test_stream_empty_response_still_complete_shape():
    tr = convert.OpenAIStreamToAnthropic("claude")
    events = _parse_events(tr.finish())
    assert [e[0] for e in events] == [
        "message_start", "content_block_start", "content_block_stop", "message_delta", "message_stop",
    ]


def test_stream_usage_picked_from_last_chunk():
    tr = convert.OpenAIStreamToAnthropic("claude")
    tr.feed({"choices": [{"delta": {"content": "x"}}]})
    tr.feed({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})
    tail = _parse_events(tr.finish())
    # message_delta 里的 output_tokens 是 20
    msg_delta = [e for e in tail if e[0] == "message_delta"][0]
    assert msg_delta[1]["usage"]["output_tokens"] == 20
