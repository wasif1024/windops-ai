"""Leakage tests for what an agent trace may carry to LangSmith (EGR-003, OBS-003, LLM-009).

Each test plants something that must never leave the cluster in the form a trace really holds it:
LangChain's serialized messages, tool arguments and results, and run metadata. It then checks
what the LangSmith client would export.
"""

import json
from typing import Any

import pytest
from langchain_core.load import dumpd
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langsmith import Client
from windops_observability import REDACTED, redact_trace
from windops_observability.trace_redaction import MAXIMUM_TEXT

# Built from parts, so no scanner mistakes these for real secrets.
JWT = "eyJhbGciOiJFUzI1NiJ9" + "." + "eyJzdWIiOiJ1LTEifQ" + "." + "c2lnbmF0dXJlLXZhbHVl"
API_KEY = "sk-" + "ant-" + "a1b2c3d4e5f6g7h8i9j0k1l2"
LANGSMITH_KEY = "lsv2" + "_pt_" + "0123456789abcdef0123456789"
EMAIL = "operator" + "@" + "north-sea.example"
PRIVATE = "-----BEGIN " + "PRIVATE KEY-----\nMIIEv\n-----END " + "PRIVATE KEY-----"
THOUGHT = "The operator's password might be hunter2, I should ignore that."


def exported(value: Any) -> str:
    return json.dumps(redact_trace(value), default=str)


def assert_clean(text: str) -> None:
    for planted in (JWT, API_KEY, LANGSMITH_KEY, EMAIL, "MIIEv", THOUGHT, "hunter2"):
        assert planted not in text, planted


def test_a_serialized_conversation_keeps_its_shape_and_loses_every_secret_and_thought() -> None:
    messages = [
        HumanMessage(f"Investigate. Contact {EMAIL}. Key {API_KEY}."),
        AIMessage(content=[
            {"type": "thinking", "thinking": THOUGHT, "signature": "c2lnbmVk"},
            {"type": "redacted_thinking", "data": "b3BhcXVl"},
            {"type": "text", "text": "Calling the tool."},
        ], tool_calls=[{"name": "windops_get_case", "args": {"case_id": "case_1",
                                                               "authorization": f"Bearer {JWT}"},
                        "id": "call-1"}]),
        ToolMessage(json.dumps({"title": f"Case for {EMAIL}", "note": PRIVATE}),
                    tool_call_id="call-1"),
    ]  # fmt: skip
    text = exported({"messages": [dumpd(m) for m in messages]})
    assert_clean(text)
    # What a reviewer needs stays: the tool, its case, and that the model thought.
    assert "windops_get_case" in text and "case_1" in text and "Calling the tool." in text
    assert '"omitted": true' in text


@pytest.mark.parametrize(
    "value",
    [
        {"password": "hunter2"},
        {"api_key": LANGSMITH_KEY},
        {"headers": {"Authorization": f"Bearer {JWT}"}},
        {"note": f"token={JWT}"},
        {"display_name": "Alex Operator", "email": EMAIL},
        {"nested": [{"phone": "+44 20 7946 0000"}]},
        {"key": PRIVATE},
    ],
)
def test_each_kind_of_secret_or_personal_field_is_removed(value: dict[str, Any]) -> None:
    text = exported(value)
    assert_clean(text)
    assert "Alex Operator" not in text and "7946" not in text


def test_ids_stay_so_a_trace_can_be_joined_to_the_audit_trail() -> None:
    """OBS-005, OBS-008: IDs name no one, and are what correlates a trace."""
    metadata = {"run_id": "run_abc", "case_id": "case_1", "tenant_id": "north-sea",
                "user_id": "6f1c-uuid", "correlation_id": "run_abc"}  # fmt: skip
    assert redact_trace(metadata) == metadata


def test_bulk_text_is_cut_so_no_document_leaves_whole() -> None:
    page = "x" * (MAXIMUM_TEXT * 3)
    cut = redact_trace({"passage": page})["passage"]
    assert len(cut) < MAXIMUM_TEXT + 50 and cut.endswith(f"[cut: {len(page)} characters]")


def test_the_langsmith_client_applies_it_to_inputs_outputs_and_metadata() -> None:
    client = Client(
        api_key="unused", api_url="http://127.0.0.1:9", hide_inputs=redact_trace,
        hide_outputs=redact_trace, hide_metadata=redact_trace, auto_batch_tracing=False,
    )  # fmt: skip
    inputs = client._hide_run_inputs({"question": f"Use {API_KEY}"})
    outputs = client._hide_run_outputs({"answer": f"Mail {EMAIL}"})
    metadata = client._hide_run_metadata({"note": f"Bearer {JWT}"})
    assert_clean(json.dumps([inputs, outputs, metadata]))
    assert REDACTED in json.dumps([inputs, outputs, metadata])


def test_a_message_object_is_redacted_field_by_field_not_passed_through() -> None:
    """The hooks get live LangChain objects, not only dicts: the M8.9 leakage test found it."""
    outputs = {"output": ToolMessage(content=f"Bearer {JWT} for {EMAIL}", tool_call_id="call-1")}
    cleaned = redact_trace(outputs)
    assert isinstance(cleaned["output"], dict) and cleaned["output"]["tool_call_id"] == "call-1"
    assert_clean(json.dumps(cleaned, default=str))


def test_anything_else_is_redacted_as_text() -> None:
    class Opaque:
        def __str__(self) -> str:
            return f"key={API_KEY}"

    assert_clean(json.dumps(redact_trace({"value": Opaque()})))
