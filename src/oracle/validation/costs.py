"""Transaction-cost model shared by discovery, the backtester and paper trading.

Cost of one fill, in basis points of notional:
    commission + half spread + slippage + impact_coefficient * sqrt(participation)
where participation = order quantity / bar volume. Short positions also pay borrow, accrued
per day held. Defaults are deliberately conservative placeholders for liquid US ETFs and must
be calibrated against paper-trading fills before any result is trusted.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, ConfigDict, Field

from oracle.common.hashing import sha256_json


class CostModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    commission_bps: float = Field(default=0.5, ge=0)
    half_spread_bps: float = Field(default=1.0, ge=0)
    slippage_bps: float = Field(default=0.5, ge=0)
    impact_coefficient_bps: float = Field(default=10.0, ge=0)
    borrow_bps_per_year: float = Field(default=50.0, ge=0)
    max_participation: float = Field(default=0.05, gt=0, le=1)

    @property
    def id(self) -> str:
        return "costs-" + sha256_json(self.model_dump(mode="json"))[:16]

    def fill_cost_bps(self, participation: float) -> float:
        p = min(max(participation, 0.0), 1.0)
        return (
            self.commission_bps
            + self.half_spread_bps
            + self.slippage_bps
            + self.impact_coefficient_bps * math.sqrt(p)
        )

    def round_trip_bps(self, participation: float = 0.0) -> float:
        return 2.0 * self.fill_cost_bps(participation)

    def borrow_bps(self, days_held: float) -> float:
        return self.borrow_bps_per_year * max(days_held, 0.0) / 365.0

    def scaled(self, factor: float) -> CostModel:
        """Cost stress: every cost component multiplied by `factor`."""
        data = self.model_dump()
        for key in (
            "commission_bps",
            "half_spread_bps",
            "slippage_bps",
            "impact_coefficient_bps",
            "borrow_bps_per_year",
        ):
            data[key] *= factor
        return CostModel(**data)
