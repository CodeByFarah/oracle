"""Provider-agnostic interfaces.

A provider does two things, kept separate so raw data is always preserved unmodified:
1. `fetch_*` returns `RawPayload`s, the exact bytes the source returned.
2. `to_canonical` maps one payload onto a canonical dataset contract (oracle.schemas.market).

Providers never write to storage and never decide what is stored; ingestion does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal, Protocol, runtime_checkable

import pyarrow as pa
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from oracle.schemas.market import DatasetKind


class ProviderUnavailable(RuntimeError):
    """The provider is disabled (missing free credential, optional package, or cost guard)."""


class ProviderRequestError(RuntimeError):
    """The upstream request failed. Messages never contain credentials."""


@dataclass(frozen=True)
class RawPayload:
    provider: str
    dataset: DatasetKind
    content: bytes
    content_type: str
    retrieved_at: datetime
    # Request parameters that identify this payload. Must never contain credentials.
    request: tuple[tuple[str, str], ...] = field(default=())

    def param(self, name: str) -> str:
        for key, value in self.request:
            if key == name:
                return value
        raise KeyError(f"payload from {self.provider} has no request parameter '{name}'")


class ProviderCapabilities(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    datasets: frozenset[DatasetKind]
    requires_api_key: bool
    requires_payment: bool
    synthetic: bool
    point_in_time: bool
    license_note: str


class ProviderStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: ProviderCapabilities
    enabled: bool
    reason: str | None = None


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


class BarRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    symbols: tuple[str, ...] = Field(min_length=1)
    start: datetime
    end: datetime
    interval: Literal["1m", "1d"]

    @field_validator("start", "end")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def _ordered(self) -> BarRequest:
        if self.end <= self.start:
            raise ValueError("end must be after start")
        return self


class MacroSeriesRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    series_ids: tuple[str, ...] = Field(min_length=1)
    observation_start: date | None = None
    observation_end: date | None = None
    # True: every historical vintage (ALFRED), required for point-in-time research.
    all_vintages: bool = True


_CIK_RE = re.compile(r"^\d{1,10}$")


class FilingsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    ciks: tuple[str, ...] = Field(min_length=1)
    include_older_pages: bool = False

    @field_validator("ciks")
    @classmethod
    def _pad(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        for v in values:
            if not _CIK_RE.fullmatch(v):
                raise ValueError(f"CIK must be 1-10 digits, got {v!r}")
        return tuple(v.zfill(10) for v in values)


@runtime_checkable
class DataProvider(Protocol):
    capabilities: ProviderCapabilities

    def to_canonical(self, payload: RawPayload) -> pa.Table: ...


@runtime_checkable
class HistoricalBarProvider(DataProvider, Protocol):
    def fetch_bars(self, request: BarRequest) -> list[RawPayload]: ...


@runtime_checkable
class MacroDataProvider(DataProvider, Protocol):
    def fetch_macro(self, request: MacroSeriesRequest) -> list[RawPayload]: ...


@runtime_checkable
class FilingsProvider(DataProvider, Protocol):
    def fetch_filings(self, request: FilingsRequest) -> list[RawPayload]: ...


# Interfaces whose canonical contracts arrive in later phases. Declared now so adapters can be
# written against them; no implementations exist yet (status: pending).


class OptionsChainRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    underlying: str
    as_of: date


class OptionsDataProvider(DataProvider, Protocol):
    def fetch_options_chain(self, request: OptionsChainRequest) -> list[RawPayload]: ...


class CorporateActionsProvider(DataProvider, Protocol):
    def fetch_corporate_actions(self, request: BarRequest) -> list[RawPayload]: ...


class ReferenceDataProvider(DataProvider, Protocol):
    def fetch_reference(self, symbols: tuple[str, ...], as_of: date) -> list[RawPayload]: ...


class NewsProvider(DataProvider, Protocol):
    def fetch_news(self, start: datetime, end: datetime) -> list[RawPayload]: ...


class RealtimeDataProvider(Protocol):
    capabilities: ProviderCapabilities

    def subscribe(self, symbols: tuple[str, ...]) -> object: ...
