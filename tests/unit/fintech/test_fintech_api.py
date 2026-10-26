from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from oracle.api.app import create_app
from tests.unit.fintech.conftest import NOW, settings_for

H = {"X-Oracle-Client": "dashboard"}


@pytest.fixture
def client(seeded_root: Path) -> TestClient:
    return TestClient(create_app(settings_for(seeded_root), clock=lambda: NOW))


def test_state_changes_need_the_client_header(client: TestClient) -> None:
    r = client.post("/api/fintech/sandbox/simulate", json={})
    assert r.status_code == 403
    r = client.post(
        "/api/fintech/sandbox/simulate", json={}, headers={**H, "Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    assert client.post("/api/fintech/sandbox/simulate", json={}, headers=H).status_code == 200


def test_non_loopback_host_is_refused(client: TestClient) -> None:
    assert client.get("/api/health", headers={"Host": "attacker.example:8765"}).status_code == 421
    assert client.get("/api/health", headers={"Host": "127.0.0.1:8765"}).status_code == 200


def test_security_headers(client: TestClient) -> None:
    r = client.get("/api/fintech/meta")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


def test_overview_and_lists(client: TestClient) -> None:
    o = client.get("/api/fintech/overview").json()
    assert {
        "totals",
        "health",
        "accounts",
        "cards",
        "recent",
        "insights",
        "cashflow",
        "daily",
    } <= set(o)
    assert o["customer"]["synthetic"] is True
    page = client.get("/api/fintech/transactions?category=dining&limit=5").json()
    assert page["total"] > 5
    assert len(page["items"]) == 5
    assert all(t["category"] == "dining" for t in page["items"])
    assert client.get("/api/fintech/transactions?flagged=true").json()["total"] >= 4
    for path in (
        "accounts",
        "cards",
        "analytics",
        "insights",
        "budgets",
        "goals",
        "payees",
        "payments",
        "alerts",
        "security",
        "reconcile",
        "meta",
    ):
        assert client.get(f"/api/fintech/{path}").status_code == 200, path


def test_card_lifecycle_over_http(client: TestClient) -> None:
    account = client.get("/api/fintech/accounts").json()[0]["id"]
    card = client.post(
        "/api/fintech/cards", json={"account_id": account, "nickname": "Trips"}, headers=H
    ).json()
    patched = client.patch(
        f"/api/fintech/cards/{card['id']}",
        json={"international": False, "monthly_minor": 50_000, "blocked_categories": ["gambling"]},
        headers=H,
    ).json()
    assert patched["controls"]["international"] is False
    assert patched["limits"]["monthly_minor"] == 50_000
    frozen = client.post(
        f"/api/fintech/cards/{card['id']}/status", json={"status": "frozen"}, headers=H
    )
    assert frozen.json()["status"] == "frozen"
    bad = client.patch(
        f"/api/fintech/cards/{card['id']}", json={"blocked_categories": ["x"]}, headers=H
    )
    assert bad.status_code == 422
    assert client.get("/api/fintech/cards/CARD-x").status_code in (404, 405)
    assert (
        client.post(
            "/api/fintech/cards/CARD-x/status", json={"status": "frozen"}, headers=H
        ).status_code
        == 404
    )


def test_budget_round_trip(client: TestClient) -> None:
    rows = client.put("/api/fintech/budgets/travel", json={"limit_minor": 50_000}, headers=H).json()
    assert any(b["category"] == "travel" for b in rows)
    assert (
        client.put("/api/fintech/budgets/income", json={"limit_minor": 1}, headers=H).status_code
        == 422
    )
    rows = client.delete("/api/fintech/budgets/travel", headers=H).json()
    assert not any(b["category"] == "travel" for b in rows)


def test_offline_chat_streams_ndjson(client: TestClient) -> None:
    status = client.get("/api/ai/status").json()
    assert status["engine"] == "offline"
    assert status["local_only"] is True
    with client.stream(
        "POST", "/api/ai/chat", json={"message": "What subscriptions do I have?"}, headers=H
    ) as r:
        assert r.headers["content-type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in r.iter_lines() if line]
    assert events[0]["type"] == "meta"
    assert events[-1]["type"] == "done"
    text = "".join(e["text"] for e in events if e["type"] == "token")
    assert "Streamly" in text
    conv = events[-1]["conversation_id"]
    assert len(client.get(f"/api/ai/conversations/{conv}").json()["messages"]) == 2
    assert client.get("/api/ai/conversations/CONV-nope").status_code == 404


def test_ai_features_without_a_model(client: TestClient) -> None:
    assert client.post("/api/ai/briefing", headers=H).json()["engine"] == "offline"
    txn = client.get("/api/fintech/transactions?status=declined&limit=1").json()["items"][0]
    out = client.post(f"/api/ai/transactions/{txn['id']}/explain", headers=H).json()
    assert out["text"]
    assert client.post("/api/ai/transactions/TXN-nope/explain", headers=H).status_code == 404


def test_dashboard_assets_are_served(client: TestClient) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "/static/app.js" in page.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/app.css").status_code == 200
