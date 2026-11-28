"""LangSmith down: traces are dropped, the Audit Service stays authoritative, and an alert says so.

SRS 7.7, RES-005.
"""

import inspect
import logging
from collections.abc import Iterator

import langsmith.client
import pytest
from langchain_core.runnables import RunnableLambda
from windops_agent_runtime.tracing import (
    FAILED_SENDS,
    LANGSMITH_LOGGER,
    DroppedTraces,
    Tracer,
    count_dropped_traces,
    redacting_client,
)


@pytest.fixture
def counted() -> Iterator[list[int]]:
    seen: list[int] = []
    target = logging.getLogger(LANGSMITH_LOGGER)
    saved = list(target.handlers)
    target.handlers[:] = []
    count_dropped_traces(DroppedTraces(lambda: seen.append(1)))
    yield seen
    target.handlers[:] = saved


def test_the_library_still_reports_a_failed_send_as_counted() -> None:
    """A newer LangSmith that words it differently fails here, not silently in production."""
    source = inspect.getsource(langsmith.client)
    for message in FAILED_SENDS:
        assert f'logger.warning(f"{message}:' in source, message


@pytest.mark.parametrize("message", FAILED_SENDS)
def test_each_failed_send_is_counted(counted: list[int], message: str) -> None:
    logging.getLogger(LANGSMITH_LOGGER).warning(f"{message}: ConnectError")
    assert counted == [1]


def test_other_warnings_are_not_counted(counted: list[int]) -> None:
    logging.getLogger(LANGSMITH_LOGGER).warning("Invalid value for BATCH_INGEST_SIZE_LIMIT")
    assert counted == []


def test_counting_is_set_up_once(counted: list[int]) -> None:
    count_dropped_traces()
    logging.getLogger(LANGSMITH_LOGGER).warning(f"{FAILED_SENDS[0]}: x")
    assert counted == [1]


def test_an_unreachable_langsmith_drops_and_counts_without_holding_up_the_run(
    counted: list[int],
) -> None:
    tracer = Tracer(redacting_client("lsv2_test", api_url="http://127.0.0.1:9"), "check")
    with tracer.tracing("run_check", ["check"]):

        def answer(question: str) -> str:
            return f"answer to {question}"

        assert RunnableLambda(answer).invoke("x") == "answer to x"
    tracer.flush()
    assert counted, "the failed send was counted"
