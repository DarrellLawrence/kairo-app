"""Tool calls a voice model writes as text (owner's call 2026-10-20: after "Hello" Kairo said
`<|tool_call_start|>[ask_kairo()]<|tool_call_end|>` out loud). LFM2 writes Pythonic calls between
<|tool_call_start|> and <|tool_call_end|>; Hermes/Qwen write JSON in <tool_call> tags; llama-server
passes both through as plain content for such models. VoiceModel.stream takes them out of the words
(nothing of a marker is ever spoken, even split across pieces) and gives the same tool_call events as
the OpenAI-style path. A call is only valid if the tool was offered on that turn and its required
arguments are there and not empty; anything else is dropped without a word (logged: name and reason)."""

from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from app.brain.voice_local import ASK_KAIRO, VoiceModel


def _sse(chunks: list[dict]) -> bytes:
    lines = [f"data: {json.dumps(c)}\n\n" for c in chunks]
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode("utf-8")


def _content(*pieces: str) -> list[dict]:
    return [{"choices": [{"index": 0, "delta": {"content": p}, "finish_reason": None}]} for p in pieces] + [
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}]


def stream(tmp_path, chunks: list[dict], tools=None) -> list[dict]:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=_sse(chunks)))
    vm = VoiceModel(lambda: None, log_dir=tmp_path, transport=transport)

    async def go():
        return [ev async for ev in vm.stream([{"role": "user", "content": "x"}], tools=tools)]
    return asyncio.run(go())


def spoken(evs) -> str:
    return "".join(e["text"] for e in evs if e["kind"] == "text")


def calls(evs) -> list[tuple[str, dict]]:
    return [(e["name"], json.loads(e["arguments"])) for e in evs if e["kind"] == "tool_call"]


LFM2 = '<|tool_call_start|>[ask_kairo(request="what\'s on tomorrow")]<|tool_call_end|>'


def test_lfm2_pythonic_call_becomes_a_tool_call_and_nothing_is_spoken(tmp_path):
    evs = stream(tmp_path, _content("<", "|tool_", "call_start|>[ask_kairo(req", "uest=\"what's on tomorrow\")]",
                                    "<|tool_call", "_end|>"), tools=[ASK_KAIRO])
    assert spoken(evs) == ""
    assert calls(evs) == [("ask_kairo", {"request": "what's on tomorrow"})]
    kinds = [e["kind"] for e in evs]
    assert kinds.index("tool_start") < kinds.index("tool_call") and kinds[-1] == "done"   # the holding line comes early


def test_no_piece_of_a_marker_is_ever_spoken_even_one_character_at_a_time(tmp_path):
    text = "Sure. " + LFM2 + " One sec."
    evs = stream(tmp_path, _content(*list(text)), tools=[ASK_KAIRO])
    assert spoken(evs) == "Sure.  One sec."
    assert not any("<" in e["text"] or "|" in e["text"] for e in evs if e["kind"] == "text")
    assert calls(evs) == [("ask_kairo", {"request": "what's on tomorrow"})]


def test_the_hello_bug_a_call_that_was_not_offered_is_dropped_silently(tmp_path, caplog):
    """Small talk offers no tool: the model's call anyway is never spoken, never shown, never run."""
    with caplog.at_level(logging.INFO, logger="kairo.voice_llm"):
        evs = stream(tmp_path, _content("<|tool_call_start|>[ask_kairo()]", "<|tool_call_end|>"), tools=None)
    assert spoken(evs) == "" and calls(evs) == [] and "tool_start" not in [e["kind"] for e in evs]
    assert any("ask_kairo" in r.getMessage() and "not offered" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("body", ['[ask_kairo()]', '[ask_kairo(request="")]', '[ask_kairo(request="   ")]',
                                  '[ask_kairo(item="L1")]', '[ask_kairo(request=None)]'])
def test_ask_kairo_without_a_request_is_invalid(tmp_path, caplog, body):
    with caplog.at_level(logging.INFO, logger="kairo.voice_llm"):
        evs = stream(tmp_path, _content(f"<|tool_call_start|>{body}<|tool_call_end|>"), tools=[ASK_KAIRO])
    assert calls(evs) == [] and spoken(evs) == ""
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "ask_kairo" in msgs and "request" in msgs


def test_a_tool_that_was_not_offered_is_dropped_and_the_log_has_no_words(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="kairo.voice_llm"):
        evs = stream(tmp_path, _content('Okay. <|tool_call_start|>[send_mail(to="x@example.com", body="the secret plan")]'
                                        '<|tool_call_end|>'), tools=[ASK_KAIRO])
    assert spoken(evs) == "Okay. " and calls(evs) == []
    log_text = " ".join(r.getMessage() for r in caplog.records)
    assert "send_mail" in log_text and "secret plan" not in log_text and "x@example.com" not in log_text


def test_string_literals_with_quotes_and_escapes(tmp_path):
    evs = stream(tmp_path, _content('<|tool_call_start|>[ask_kairo(request="tell Lea \\"yes\\" for Friday", item=\'L1\')]'
                                    '<|tool_call_end|>'), tools=[ASK_KAIRO])
    assert calls(evs) == [("ask_kairo", {"request": 'tell Lea "yes" for Friday', "item": "L1"})]
    evs = stream(tmp_path, _content("<|tool_call_start|>[ask_kairo('it\\'s late, remind me at 7')]<|tool_call_end|>"),
                 tools=[ASK_KAIRO])
    assert calls(evs) == [("ask_kairo", {"request": "it's late, remind me at 7"})]   # positional: the first parameter


def test_code_in_a_call_is_never_run(tmp_path):
    evs = stream(tmp_path, _content("<|tool_call_start|>[ask_kairo(request=__import__('os').getcwd())]<|tool_call_end|>"),
                 tools=[ASK_KAIRO])
    assert calls(evs) == [] and spoken(evs) == ""


def test_hermes_and_qwen_json_in_tags(tmp_path):
    evs = stream(tmp_path, _content('<tool_call>\n{"name": "ask_kairo", ', '"arguments": {"request": "check my mail"}}\n',
                                    "</tool_call>"), tools=[ASK_KAIRO])
    assert spoken(evs) == "" and calls(evs) == [("ask_kairo", {"request": "check my mail"})]
    evs = stream(tmp_path, _content('<tool_call>{"name": "ask_kairo", "arguments": "{\\"request\\": \\"any mail?\\"}"}'
                                    '</tool_call>'), tools=[ASK_KAIRO])
    assert calls(evs) == [("ask_kairo", {"request": "any mail?"})]          # arguments as a JSON string too
    evs = stream(tmp_path, _content('<tool_call>{"name": "ask_kairo", "arguments": {}}</tool_call>'), tools=[ASK_KAIRO])
    assert calls(evs) == []


def test_other_special_tokens_are_never_spoken_but_a_plain_angle_bracket_is(tmp_path):
    evs = stream(tmp_path, _content("Two <", " three, ", "yes.<|im_", "end|>"), tools=None)
    assert spoken(evs) == "Two < three, yes."


def test_a_call_cut_off_at_the_end_is_dropped(tmp_path):
    evs = stream(tmp_path, _content('<|tool_call_start|>[ask_kairo(request="what'), tools=[ASK_KAIRO])
    assert spoken(evs) == "" and calls(evs) == []


def test_the_openai_style_path_is_checked_the_same_way(tmp_path, caplog):
    def tc(name, args):
        return [{"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                                                     "function": {"name": name, "arguments": ""}}]}}]},
                {"choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": args}}]}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}]
    ok = stream(tmp_path, tc("ask_kairo", '{"request": "any mail?"}'), tools=[ASK_KAIRO])
    assert calls(ok) == [("ask_kairo", {"request": "any mail?"})] and "tool_start" in [e["kind"] for e in ok]
    with caplog.at_level(logging.INFO, logger="kairo.voice_llm"):
        empty = stream(tmp_path, tc("ask_kairo", "{}"), tools=[ASK_KAIRO])
        offered_none = stream(tmp_path, tc("ask_kairo", '{"request": "hi"}'), tools=None)
        other = stream(tmp_path, tc("delete_everything", '{"request": "hi"}'), tools=[ASK_KAIRO])
    assert calls(empty) == calls(offered_none) == calls(other) == []
    assert "tool_start" not in [e["kind"] for e in offered_none + other]   # no holding line for a tool it can't use
