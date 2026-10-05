"""Rate-limited HTTP GET with bounded retries and credential-safe error messages."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping

import httpx

from oracle.data.providers.base import ProviderRequestError

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})
_SECRET_PARAMS = frozenset({"api_key", "apikey", "key", "token"})


def redact_url(url: httpx.URL) -> str:
    params = [
        (k, "***" if k.lower() in _SECRET_PARAMS else v) for k, v in url.params.multi_items()
    ]
    return str(url.copy_with(params=params))


class RateLimitedHttp:
    def __init__(
        self,
        client: httpx.Client,
        *,
        min_interval_s: float,
        max_retries: int = 3,
        backoff_s: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._min_interval = min_interval_s
        self._max_retries = max_retries
        self._backoff = backoff_s
        self._sleep = sleep
        self._clock = clock
        self._last: float | None = None

    def _throttle(self) -> None:
        if self._last is not None:
            wait = self._min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def get(self, url: str, params: Mapping[str, str] | None = None) -> httpx.Response:
        for attempt in range(self._max_retries + 1):
            self._throttle()
            try:
                response = self._client.get(url, params=params)
            except httpx.TransportError as exc:
                if attempt < self._max_retries:
                    self._sleep(self._backoff * 2**attempt)
                    continue
                safe = redact_url(httpx.URL(url, params=params))
                raise ProviderRequestError(
                    f"transport error for {safe}: {type(exc).__name__}"
                ) from None
            if response.status_code in RETRYABLE_STATUS and attempt < self._max_retries:
                self._sleep(self._backoff * 2**attempt)
                continue
            if response.is_error:
                raise ProviderRequestError(
                    f"HTTP {response.status_code} for {redact_url(response.request.url)}"
                )
            return response
        raise AssertionError("unreachable")  # pragma: no cover
