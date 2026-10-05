"""The execution gateway: the only path from a decision to a broker.

Pipeline (each step can stop the order; every outcome is audited):
1. deterministic order validation;
2. kill switch (blocks risk-increasing orders while engaged);
3. risk engine (authority to reject);
4. execution policy (participation cap against the latest bar volume);
5. broker submission.
No language model, agent or strategy object holds a broker reference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from oracle.execution.brokers import Broker
from oracle.execution.orders import Order, OrderValidator
from oracle.risk.engine import PortfolioState, ProposedOrder, RiskEngine


class AuditSink(Protocol):
    def record(self, actor: str, action: str, subject_id: str, details: dict[str, Any]) -> None: ...


@dataclass
class MemoryAudit:
    entries: list[tuple[str, str, str, dict[str, Any]]] = field(default_factory=list)

    def record(self, actor: str, action: str, subject_id: str, details: dict[str, Any]) -> None:
        self.entries.append((actor, action, subject_id, details))


class KillSwitch:
    """Global stop. File-backed when given a path, so an operator can engage it out of band."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._engaged = False

    @property
    def engaged(self) -> bool:
        return self._engaged or (self.path is not None and self.path.exists())

    def engage(self, reason: str) -> None:
        self._engaged = True
        if self.path is not None:
            self.path.write_text(reason, encoding="utf-8")

    def disengage(self) -> None:
        self._engaged = False
        if self.path is not None and self.path.exists():
            self.path.unlink()


class GatewayResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    stage: str  # where the order stopped, or "submitted"
    reasons: tuple[str, ...]
    broker_order_id: str | None = None


def _rejected(stage: str, *reasons: str) -> GatewayResult:
    return GatewayResult(accepted=False, stage=stage, reasons=reasons)


class ExecutionGateway:
    def __init__(
        self,
        broker: Broker,
        validator: OrderValidator,
        risk: RiskEngine,
        audit: AuditSink,
        *,
        kill_switch: KillSwitch | None = None,
        max_participation: float = 0.05,
        actor: str = "execution-gateway",
    ) -> None:
        self._broker = broker
        self.validator = validator
        self.risk = risk
        self.audit = audit
        self.kill_switch = kill_switch or KillSwitch()
        self.max_participation = max_participation
        self.actor = actor

    def _finish(self, order: Order, result: GatewayResult) -> GatewayResult:
        details = {
            "stage": result.stage,
            "reasons": list(result.reasons),
            "strategy_id": order.strategy_id,
            "symbol": order.symbol,
            "side": order.side,
            "quantity": order.quantity,
            "reference_price": order.reference_price,
        }
        action = "order_submitted" if result.accepted else "order_rejected"
        self.audit.record(self.actor, action, order.client_order_id, details)
        return result

    def _decide(
        self, order: Order, state: PortfolioState, now: datetime, bar_volume: float | None
    ) -> GatewayResult:
        checked = self.validator.validate(order, now)
        if not checked.ok:
            return _rejected("validation", *checked.reasons)
        proposed = ProposedOrder(
            strategy_id=order.strategy_id,
            symbol=order.symbol,
            quantity=order.signed_quantity,
            price=order.reference_price,
            bar_volume=bar_volume,
        )
        decision = self.risk.check(proposed, state)
        if self.kill_switch.engaged and decision.risk_increasing:
            return _rejected("kill_switch", "kill switch engaged")
        if not decision.approved:
            return _rejected("risk", *decision.reasons)
        if bar_volume is not None and bar_volume > 0:
            share = order.quantity / bar_volume
            if share > self.max_participation:
                return _rejected(
                    "execution_policy",
                    f"order is {share:.1%} of bar volume (policy {self.max_participation:.1%})",
                )
        try:
            broker_id = self._broker.submit(order)
        except PermissionError as exc:  # live safety gates refuse by raising
            return _rejected("broker", str(exc))
        return GatewayResult(
            accepted=True, stage="submitted", reasons=decision.reasons, broker_order_id=broker_id
        )

    def submit(
        self, order: Order, state: PortfolioState, *, now: datetime, bar_volume: float | None
    ) -> GatewayResult:
        return self._finish(order, self._decide(order, state, now, bar_volume))
