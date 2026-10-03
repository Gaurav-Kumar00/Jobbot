from __future__ import annotations

import httpx
import pytest
import respx

from jobbot.http import USER_AGENT, FetchError, HttpClient

URL = "https://boards.example.com/v1/jobs"


class RecordingSleep:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@pytest.fixture
def sleep() -> RecordingSleep:
    return RecordingSleep()


@pytest.fixture
async def http(sleep):
    async with HttpClient(sleep=sleep, jitter=lambda: 0.0) as client:
        yield client


@respx.mock
async def test_get_json_sends_polite_headers(http):
    route = respx.get(URL).mock(return_value=httpx.Response(200, json={"jobs": []}))
    assert await http.get_json(URL, params={"content": "true"}) == {"jobs": []}
    request = route.calls.last.request
    assert request.headers["User-Agent"] == USER_AGENT
    assert request.url.params["content"] == "true"


@respx.mock
async def test_retries_5xx_with_exponential_backoff(http, sleep):
    respx.get(URL).mock(
        side_effect=[httpx.Response(503), httpx.Response(502), httpx.Response(200, json={"ok": 1})]
    )
    assert await http.get_json(URL) == {"ok": 1}
    assert sleep.calls == [1.0, 2.0]


@respx.mock
async def test_gives_up_after_max_retries(http, sleep):
    respx.get(URL).mock(return_value=httpx.Response(500))
    with pytest.raises(FetchError) as exc:
        await http.get_json(URL)
    assert exc.value.status_code == 500
    assert sleep.calls == [1.0, 2.0, 4.0]


@respx.mock
async def test_honours_retry_after_seconds(http, sleep):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, json={}),
        ]
    )
    await http.get_json(URL)
    assert sleep.calls == [7.0]


@respx.mock
async def test_retry_after_is_capped(http, sleep):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "86400"}),
            httpx.Response(200, json={}),
        ]
    )
    await http.get_json(URL)
    assert sleep.calls == [60.0]


@respx.mock
async def test_unparseable_retry_after_falls_back_to_backoff(http, sleep):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(503, headers={"Retry-After": "soon-ish"}),
            httpx.Response(200, json={}),
        ]
    )
    await http.get_json(URL)
    assert sleep.calls == [1.0]


@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404, 410])
async def test_client_errors_fail_fast(http, sleep, status):
    respx.get(URL).mock(return_value=httpx.Response(status))
    with pytest.raises(FetchError) as exc:
        await http.get_json(URL)
    assert exc.value.status_code == status
    assert sleep.calls == []


@respx.mock
async def test_timeouts_and_network_errors_are_retried(http, sleep):
    respx.get(URL).mock(
        side_effect=[
            httpx.ReadTimeout("slow"),
            httpx.ConnectError("refused"),
            httpx.Response(200, json=[1]),
        ]
    )
    assert await http.get_json(URL) == [1]
    assert len(sleep.calls) == 2


@respx.mock
async def test_persistent_network_failure_raises_fetch_error(http):
    respx.get(URL).mock(side_effect=httpx.ConnectTimeout("nope"))
    with pytest.raises(FetchError, match="network error"):
        await http.get_json(URL)


@respx.mock
async def test_invalid_json_is_a_fetch_error(http):
    respx.get(URL).mock(return_value=httpx.Response(200, text="<html>maintenance</html>"))
    with pytest.raises(FetchError, match="invalid JSON"):
        await http.get_json(URL)


@respx.mock
async def test_post_json(http):
    route = respx.post(URL).mock(return_value=httpx.Response(200, json={"total": 3}))
    assert await http.post_json(URL, {"limit": 20}) == {"total": 3}
    assert route.calls.last.request.content == b'{"limit":20}'


def test_fetch_error_message_omits_query_string():
    err = FetchError("https://api.example.com/search?app_key=SECRET123&q=python", "HTTP 500", 500)
    assert "SECRET123" not in str(err)
    assert "api.example.com/search" in str(err)


@respx.mock
async def test_retry_after_zero_uses_backoff(http, sleep):
    respx.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(429, headers={"Retry-After": "0"}),
            httpx.Response(200, json={}),
        ]
    )
    await http.get_json(URL)
    assert sleep.calls == [1.0, 2.0]
