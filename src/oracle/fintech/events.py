"""Customer alerts and the append-only security audit trail."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from oracle.common.ids import new_id
from oracle.fintech.models import Alert, SecurityEvent


def audit(
    session: Session,
    *,
    customer_id: str,
    actor: str,
    action: str,
    subject_type: str,
    subject_id: str | None,
    now: datetime,
    **details: Any,
) -> SecurityEvent:
    event = SecurityEvent(
        customer_id=customer_id,
        occurred_at=now,
        actor=actor,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        details=details,
    )
    session.add(event)
    return event


def raise_alert(
    session: Session,
    *,
    customer_id: str,
    kind: str,
    severity: str,
    title: str,
    body: str,
    now: datetime,
    subject_type: str | None = None,
    subject_id: str | None = None,
    dedupe: bool = False,
) -> Alert | None:
    """Create an alert. With `dedupe`, skip it if an open alert has the same kind and title."""
    if dedupe:
        existing = session.scalar(
            select(func.count())
            .select_from(Alert)
            .where(
                Alert.customer_id == customer_id,
                Alert.kind == kind,
                Alert.title == title,
                Alert.status == "open",
            )
        )
        if existing:
            return None
    alert = Alert(
        id=new_id("ALRT", now=now),
        customer_id=customer_id,
        kind=kind,
        severity=severity,
        title=title,
        body=body,
        subject_type=subject_type,
        subject_id=subject_id,
        status="open",
        created_at=now,
    )
    session.add(alert)
    return alert
