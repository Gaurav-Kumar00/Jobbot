from __future__ import annotations

import json

import httpx
import pytest
import respx

from jobbot.notify.base import Button, OutgoingMessage
from jobbot.notify.telegram import (
    MAX_MESSAGE_LEN,
    TelegramClient,
    TelegramError,
    TelegramNotifier,
)
from tests.conftest import FAKE_TOKEN

SEND_URL = f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage"


def ok(result: object) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleep() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
async def client(sleep):
    async with TelegramClient(FAKE_TOKEN, sleep=sleep) as c:
        yield c


@respx.mock
async def test_send_message_payload(client):
    route = respx.post(SEND_URL).mock(return_value=ok({"message_id": 11}))
    msg = OutgoingMessage("<b>hi</b>", buttons=(Button("Apply", "https://example.com/job/1"),))

    message_id = await TelegramNotifier(client, "42").send(msg)

    assert message_id == "11"
    payload = json.loads(route.calls.last.request.content)
    assert payload["chat_id"] == "42"
    assert payload["parse_mode"] == "HTML"
    assert payload["link_preview_options"] == {"is_disabled": True}
    assert payload["reply_markup"] == {
        "inline_keyboard": [[{"text": "Apply", "url": "https://example.com/job/1"}]]
    }


@respx.mock
async def test_no_reply_markup_without_buttons(client):
    route = respx.post(SEND_URL).mock(return_value=ok({"message_id": 1}))
    await client.send_message(42, OutgoingMessage("plain"))
    assert "reply_markup" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_rate_limit_honours_retry_after(client, sleep):
    limited = httpx.Response(
        429,
        json={
            "ok": False,
            "error_code": 429,
            "description": "Too Many Requests",
            "parameters": {"retry_after": 7},
        },
    )
    respx.post(SEND_URL).mock(side_effect=[limited, ok({"message_id": 5})])

    result = await client.send_message(42, OutgoingMessage("x"))

    assert result["message_id"] == 5
    assert sleep.calls == [7.0]


@respx.mock
async def test_retry_after_is_capped(client, sleep):
    limited = httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 3600}})
    respx.post(SEND_URL).mock(side_effect=[limited, ok({"message_id": 5})])
    await client.send_message(42, OutgoingMessage("x"))
    assert sleep.calls == [60.0]


@respx.mock
async def test_server_errors_retry_with_backoff_then_fail(client, sleep):
    respx.post(SEND_URL).mock(return_value=httpx.Response(502, text="Bad Gateway"))
    with pytest.raises(TelegramError) as exc:
        await client.send_message(42, OutgoingMessage("x"))
    assert exc.value.status_code == 502
    assert sleep.calls == [1.0, 2.0, 4.0]


@respx.mock
async def test_network_errors_retry_and_error_hides_token(client, sleep):
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("connection refused"))
    with pytest.raises(TelegramError) as exc:
        await client.send_message(42, OutgoingMessage("x"))
    assert "network error" in str(exc.value)
    assert FAKE_TOKEN not in str(exc.value)
    assert exc.value.__cause__ is None
    assert len(sleep.calls) == 3


@respx.mock
async def test_client_errors_fail_fast_with_description(client, sleep):
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        )
    )
    with pytest.raises(TelegramError, match="chat not found"):
        await client.send_message(42, OutgoingMessage("x"))
    assert sleep.calls == []


@respx.mock
async def test_non_json_error_body(client):
    respx.post(SEND_URL).mock(return_value=httpx.Response(404, text="<html>nope</html>"))
    with pytest.raises(TelegramError) as exc:
        await client.send_message(42, OutgoingMessage("x"))
    assert exc.value.status_code == 404


async def test_rejects_oversized_message(client):
    with pytest.raises(ValueError):
        await client.send_message(42, OutgoingMessage("x" * (MAX_MESSAGE_LEN + 1)))


@respx.mock
async def test_connect_failures_are_known_undelivered(client):
    respx.post(SEND_URL).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(TelegramError) as exc:
        await client.send_message(42, OutgoingMessage("x"))
    assert exc.value.maybe_delivered is False


@respx.mock
async def test_read_timeouts_may_have_been_delivered(client):
    respx.post(SEND_URL).mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(TelegramError) as exc:
        await client.send_message(42, OutgoingMessage("x"))
    assert exc.value.maybe_delivered is True


@respx.mock
async def test_api_errors_are_not_delivered(client):
    respx.post(SEND_URL).mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "Bad Request"})
    )
    with pytest.raises(TelegramError) as exc:
        await client.send_message(42, OutgoingMessage("x"))
    assert exc.value.maybe_delivered is False
