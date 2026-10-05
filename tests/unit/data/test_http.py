from __future__ import annotations

import httpx
import pytest

from oracle.data.http import RateLimitedHttp
from oracle.data.providers.base import ProviderRequestError
from tests.unit.data.helpers import RecordingTransport

URL = "https://example.invalid/data"


class FakeTime:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def _http(
    transport: RecordingTransport,
    fake: FakeTime,
    *,
    min_interval_s: float = 0.0,
    max_retries: int = 3,
) -> RateLimitedHttp:
    return RateLimitedHttp(
        httpx.Client(transport=transport),
        min_interval_s=min_interval_s,
        max_retries=max_retries,
        backoff_s=1.0,
        sleep=fake.sleep,
        clock=fake.clock,
    )


def test_retries_transient_errors_with_exponential_backoff() -> None:
    statuses = iter([503, 429, 200])
    transport = RecordingTransport(lambda r: httpx.Response(next(statuses), text="ok"))
    fake = FakeTime()
    response = _http(transport, fake).get(URL)
    assert response.status_code == 200
    assert len(transport.requests) == 3
    assert fake.sleeps == [1.0, 2.0]


def test_gives_up_after_max_retries_and_redacts_secrets() -> None:
    transport = RecordingTransport(lambda r: httpx.Response(503))
    with pytest.raises(ProviderRequestError) as excinfo:
        _http(transport, FakeTime(), max_retries=2).get(URL, {"api_key": "SECRET123", "q": "x"})
    assert len(transport.requests) == 3
    assert "SECRET123" not in str(excinfo.value)
    assert "q=x" in str(excinfo.value)


def test_client_errors_are_not_retried() -> None:
    transport = RecordingTransport(lambda r: httpx.Response(404))
    with pytest.raises(ProviderRequestError, match="HTTP 404"):
        _http(transport, FakeTime()).get(URL)
    assert len(transport.requests) == 1


def test_transport_errors_retry_then_fail_without_leaking_secrets() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    transport = RecordingTransport(broken)
    with pytest.raises(ProviderRequestError) as excinfo:
        _http(transport, FakeTime(), max_retries=1).get(URL, {"api_key": "SECRET123"})
    assert len(transport.requests) == 2
    assert "SECRET123" not in str(excinfo.value)
    assert excinfo.value.__cause__ is None


def test_requests_are_spaced_by_min_interval() -> None:
    transport = RecordingTransport(lambda r: httpx.Response(200))
    fake = FakeTime()
    http = _http(transport, fake, min_interval_s=0.5)
    http.get(URL)
    http.get(URL)
    assert fake.sleeps == [0.5]
