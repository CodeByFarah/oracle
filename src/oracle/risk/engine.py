"""Deterministic pre-trade risk engine with authority to reject orders.

Every order is evaluated against the post-trade portfolio. Risk-REDUCING orders (that shrink a
position's absolute size) are always allowed so the book can be flattened, even after a limit
breach. Risk-INCREASING orders are rejected when any limit would be exceeded, and all of them
are refused while the daily-loss or drawdown kill condition is active.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

EPS = 1e-12


class RiskConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    max_position_weight: float = Field(default=0.10, gt=0)
    max_gross_leverage: float = Field(default=1.0, gt=0)
    max_net_exposure: float = Field(default=0.30, ge=0)
    max_sector_weight: float = Field(default=0.30, gt=0)
    max_beta_exposure: float = Field(default=0.30, ge=0)
    max_participation: float = Field(default=0.05, gt=0, le=1)
    max_daily_loss: float = Field(default=0.02, gt=0, lt=1)
    max_drawdown: float = Field(default=0.10, gt=0, lt=1)
    max_stress_loss: float = Field(default=0.05, gt=0, lt=1)
    sector_map: dict[str, str] = Field(default_factory=dict)


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    market_shock: float  # applied through each symbol's beta
    symbol_shocks: dict[str, float] = Field(default_factory=dict)  # added on top


DEFAULT_SCENARIOS: tuple[Scenario, ...] = (
    Scenario(name="market -5%", market_shock=-0.05),
    Scenario(name="market +5%", market_shock=0.05),
    Scenario(name="market -10% crash", market_shock=-0.10),
)


class PortfolioState(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    equity: float = Field(gt=0)
    start_of_day_equity: float = Field(gt=0)
    peak_equity: float = Field(gt=0)
    positions: dict[str, float]  # signed shares
    prices: dict[str, float]
    betas: dict[str, float] = Field(default_factory=dict)  # default beta 1.0


class ProposedOrder(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy_id: str
    symbol: str
    quantity: float  # signed shares
    price: float = Field(gt=0)
    bar_volume: float | None = None


class RiskDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    approved: bool
    risk_increasing: bool
    reasons: tuple[str, ...]
    kill_active: bool


def stress_return(weights: dict[str, float], betas: dict[str, float], scenario: Scenario) -> float:
    """Portfolio return under a scenario (negative = loss), as a fraction of equity."""
    return sum(
        w * (betas.get(s, 1.0) * scenario.market_shock + scenario.symbol_shocks.get(s, 0.0))
        for s, w in weights.items()
    )


class RiskEngine:
    def __init__(
        self, config: RiskConfig, scenarios: Sequence[Scenario] = DEFAULT_SCENARIOS
    ) -> None:
        self.config = config
        self.scenarios = tuple(scenarios)

    def kill_reasons(self, state: PortfolioState) -> list[str]:
        reasons = []
        daily = state.equity / state.start_of_day_equity - 1.0
        if daily <= -self.config.max_daily_loss:
            reasons.append(f"daily loss {daily:.2%} breaches {self.config.max_daily_loss:.2%}")
        drawdown = 1.0 - state.equity / state.peak_equity
        if drawdown >= self.config.max_drawdown:
            reasons.append(f"drawdown {drawdown:.2%} breaches {self.config.max_drawdown:.2%}")
        return reasons

    def _limit_breaches(
        self, order: ProposedOrder, weights: dict[str, float], betas: dict[str, float]
    ) -> list[str]:
        cfg = self.config
        reasons = []
        w = weights.get(order.symbol, 0.0)
        if abs(w) > cfg.max_position_weight + EPS:
            reasons.append(f"position weight {w:.2%} > {cfg.max_position_weight:.2%}")
        gross = sum(abs(v) for v in weights.values())
        if gross > cfg.max_gross_leverage + EPS:
            reasons.append(f"gross leverage {gross:.2f} > {cfg.max_gross_leverage:.2f}")
        net = sum(weights.values())
        if abs(net) > cfg.max_net_exposure + EPS:
            reasons.append(f"net exposure {net:.2%} > {cfg.max_net_exposure:.2%}")
        sectors: dict[str, float] = {}
        for s, v in weights.items():
            if sector := cfg.sector_map.get(s):
                sectors[sector] = sectors.get(sector, 0.0) + v
        for sector, v in sectors.items():
            if abs(v) > cfg.max_sector_weight + EPS:
                reasons.append(f"sector {sector} weight {v:.2%} > {cfg.max_sector_weight:.2%}")
        beta = sum(v * betas.get(s, 1.0) for s, v in weights.items())
        if abs(beta) > cfg.max_beta_exposure + EPS:
            reasons.append(f"beta exposure {beta:.2f} > {cfg.max_beta_exposure:.2f}")
        if order.bar_volume is not None and order.bar_volume > 0:
            participation = abs(order.quantity) / order.bar_volume
            if participation > cfg.max_participation + EPS:
                reasons.append(f"participation {participation:.2%} > {cfg.max_participation:.2%}")
        for scenario in self.scenarios:
            outcome = stress_return(weights, betas, scenario)
            if outcome < -cfg.max_stress_loss:
                reasons.append(
                    f"stress '{scenario.name}' loss {-outcome:.2%} > {cfg.max_stress_loss:.2%}"
                )
        return reasons

    def check(self, order: ProposedOrder, state: PortfolioState) -> RiskDecision:
        if not math.isfinite(order.quantity) or order.quantity == 0:
            return RiskDecision(
                approved=False,
                risk_increasing=False,
                reasons=("invalid quantity",),
                kill_active=False,
            )
        prices = {**state.prices, order.symbol: order.price}
        before = state.positions.get(order.symbol, 0.0)
        after = {**state.positions, order.symbol: before + order.quantity}
        increasing = abs(before + order.quantity) > abs(before) + EPS
        kill = self.kill_reasons(state)
        if not increasing:
            return RiskDecision(
                approved=True,
                risk_increasing=False,
                reasons=("risk reducing",),
                kill_active=bool(kill),
            )
        weights = {s: q * prices[s] / state.equity for s, q in after.items() if q}
        reasons = [f"kill condition: {r}" for r in kill]
        reasons += self._limit_breaches(order, weights, state.betas)
        return RiskDecision(
            approved=not reasons,
            risk_increasing=True,
            reasons=tuple(reasons) or ("within limits",),
            kill_active=bool(kill),
        )
