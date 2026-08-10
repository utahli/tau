"""Tests for debug logging configuration and provider instrumentation."""

import logging

import httpx
import pytest

from tau_agent import UserMessage
from tau_ai import OpenAICompatibleConfig, OpenAICompatibleProvider
from tau_ai.openai_compatible import _mask_headers
from tau_coding.logging_config import (
    configure_debug_logging,
    is_debug_env_set,
    reset_debug_logging,
)


@pytest.fixture(autouse=True)
def _clean_logging():
    reset_debug_logging()
    yield
    reset_debug_logging()


def test_is_debug_env_set_truthy():
    for value in ("1", "true", "yes", "on", "TRUE", "On"):
        assert is_debug_env_set({"TAU_DEBUG": value})


def test_is_debug_env_set_falsy():
    for value in ("", "0", "false", "no", "off", "maybe"):
        assert not is_debug_env_set({"TAU_DEBUG": value})


def test_is_debug_env_unset():
    assert not is_debug_env_set({})


def test_configure_debug_logging_stderr_mode():
    configure_debug_logging(tui_mode=False)
    assert logging.getLogger("tau_coding.something").getEffectiveLevel() == logging.DEBUG


def test_configure_debug_logging_tui_mode(tmp_path):
    configure_debug_logging(tui_mode=True)
    root = logging.getLogger("tau_ai")
    assert root.getEffectiveLevel() == logging.DEBUG
    assert any(isinstance(h, logging.FileHandler) for h in root.handlers)


def test_configure_debug_logging_idempotent():
    configure_debug_logging(tui_mode=False)
    before = len(logging.getLogger("tau_coding").handlers)
    configure_debug_logging(tui_mode=False)
    assert before == len(logging.getLogger("tau_coding").handlers)


def test_mask_headers_redacts_authorization():
    masked = _mask_headers(
        {"Authorization": "Bearer sk-secret-key", "Content-Type": "application/json"}
    )
    assert masked["Authorization"] == "<20 chars>"
    assert masked["Content-Type"] == "application/json"


def test_mask_headers_redacts_api_key_variants():
    masked = _mask_headers({"x-api-key": "secret123", "api-key": "secret456"})
    assert "secret123" not in masked["x-api-key"]
    assert "secret456" not in masked["api-key"]


def test_mask_headers_preserves_normal_headers():
    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    assert _mask_headers(headers) == headers


async def _collect(stream):
    return [event async for event in stream]


@pytest.mark.anyio
async def test_provider_logs_request_and_sse_chunks(caplog):
    caplog.set_level(logging.DEBUG, logger="tau_ai.openai_compatible")
    sse_body = (
        'data: {"choices":[{"delta":{"content":"Hi"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"!"},"finish_reason":"stop"}]}\n\n'
        'data: [DONE]\n\n'
    )

    def handler(request):
        return httpx.Response(200, text=sse_body, headers={"content-type": "text/event-stream"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(api_key="test-key", base_url="https://example.test/v1"),
            client=client,
        )
        await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say hi")],
                tools=[],
            )
        )

    post_logs = [r for r in caplog.records if "POST" in r.message]
    assert len(post_logs) == 1
    assert "https://example.test/v1/chat/completions" in post_logs[0].message
    assert "test-model" in post_logs[0].message
    assert "test-key" not in post_logs[0].message
    sse_logs = [r for r in caplog.records if "SSE finalize parser_event" in r.message]
    assert len(sse_logs) == 1


@pytest.mark.anyio
async def test_provider_logs_http_error(caplog):
    caplog.set_level(logging.DEBUG, logger="tau_ai.openai_compatible")

    def handler(request):
        return httpx.Response(401, text='{"error":{"message":"invalid api key"}}')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(api_key="bad-key", base_url="https://example.test/v1", max_retries=0),
            client=client,
        )
        await _collect(
            provider.stream_response(
                model="test-model",
                system="You are Tau.",
                messages=[UserMessage(content="Say hi")],
                tools=[],
            )
        )

    warning_logs = [r for r in caplog.records if r.levelno == logging.WARNING and "HTTP" in r.message]
    assert len(warning_logs) == 1
    assert "401" in warning_logs[0].message
