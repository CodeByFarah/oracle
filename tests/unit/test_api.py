from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from oracle.api.app import create_app
from oracle.cli.main import app as cli_app
from oracle.common.config import Settings
from oracle.data.lake import Lake
from oracle.ingestion.pipeline import ingest_payloads
from oracle.loop.research_loop import LoopConfig, run_once
from tests.unit.data.helpers import synthetic_payloads

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def _settings(lake: Lake) -> Settings:
    return Settings(data_root=lake.root, _env_file=None)


def _client(lake: Lake) -> TestClient:
    return TestClient(create_app(_settings(lake)))


def _run_loop(lake: Lake) -> None:
    provider, payloads = synthetic_payloads(
        symbols=("AAA", "BBB", "CCC", "DDD", "EEE", "FFF"),
        start=datetime(2024, 1, 2, tzinfo=UTC),
        end=datetime(2024, 1, 12, tzinfo=UTC),
    )
    manifest = ingest_payloads(payloads, provider, lake, now=NOW).manifest
    assert manifest is not None
    run_once(lake, manifest, now=NOW, config=LoopConfig(research_budget=1, lab_permutations=20))


def test_empty_lake_is_reported_honestly(lake: Lake) -> None:
    client = _client(lake)
    health = client.get("/api/health").json()
    assert health["live_trading_enabled"] is False
    assert health["live_broker_adapter"].startswith("none")
    assert {p["name"] for p in health["providers"]} >= {"alpaca", "tiingo"}
    assert all(not d["present"] for d in client.get("/api/data").json())
    assert client.get("/api/world").status_code == 404
    for path in (
        "/api/loops",
        "/api/experiments",
        "/api/hypotheses",
        "/api/strategies",
        "/api/journal",
    ):
        assert client.get(path).json() == []


def test_artifacts_after_a_loop_cycle(lake: Lake) -> None:
    _run_loop(lake)
    client = _client(lake)
    data = client.get("/api/data?quality=true").json()
    bars = next(d for d in data if d["dataset"] == "bars")
    assert bars["present"]
    assert bars["integrity_problems"] == []
    assert "quality" in bars

    loops = client.get("/api/loops").json()
    assert len(loops) == 1
    assert loops[0]["experiments"] == 1
    assert client.get(f"/api/loops/{loops[0]['name']}").json()["experiments"] == 1

    world = client.get("/api/world").json()
    assert world["text"]
    assert world["snapshot"]["provenance"]["dataset_version"] == bars["version"]

    experiments = client.get("/api/experiments").json()
    assert len(experiments) == 1
    assert experiments[0]["validated"] == 0
    detail = client.get(f"/api/experiments/{experiments[0]['protocol_hash']}?limit=5").json()
    assert len(detail["top_tests"]) <= 5
    p_values = [t["p_value"] for t in detail["top_tests"] if t["p_value"] is not None]
    assert p_values == sorted(p_values)

    assert len(client.get("/api/journal").json()) == 1
    assert client.get("/api/journal", params={"q": "no-such-word-xyz"}).json() == []
    assert client.get("/api/strategies").json() == []

    symbols = client.get("/api/symbols").json()
    assert {s["symbol"] for s in symbols} == {"AAA", "BBB", "CCC", "DDD", "EEE", "FFF"}
    assert all(s["synthetic"] for s in symbols)
    first = symbols[0]
    series = client.get(
        "/api/bars", params={"symbol": first["symbol"], "interval": first["interval"], "limit": 50}
    ).json()
    assert series["dataset_version"] == bars["version"]
    assert series["synthetic"] is True
    rows = series["bars"]
    assert len(rows) == 50
    assert all(len(r) == 6 for r in rows)
    times = [r[0] for r in rows]
    assert times == sorted(times)  # oldest first
    assert all(r[3] <= r[4] <= r[2] for r in rows)  # low <= close <= high


@pytest.mark.parametrize(
    "params",
    [
        {"symbol": "spy;drop", "interval": "1m"},
        {"symbol": "SPY", "interval": "1 minute"},
        {"symbol": "SPY", "interval": "1m", "limit": 999999},
    ],
)
def test_bars_parameters_are_validated(lake: Lake, params: dict[str, object]) -> None:
    assert _client(lake).get("/api/bars", params=params).status_code == 422


def test_bars_without_a_dataset(lake: Lake) -> None:
    client = _client(lake)
    assert client.get("/api/symbols").json() == []
    assert client.get("/api/bars", params={"symbol": "SPY", "interval": "1m"}).status_code == 404


def test_hypotheses_show_latest_state_with_adversarial_report(lake: Lake) -> None:
    research = lake.root / "research"
    (research / "adversarial").mkdir(parents=True)
    lines = [
        {"hypothesis_id": "H-L000001", "status": "proposed"},
        {"hypothesis_id": "H-L000001", "status": "adversarial_rejected"},
    ]
    (research / "hypotheses.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    (research / "adversarial" / "H-L000001.json").write_text(json.dumps({"verdict": "rejected"}))
    items = _client(lake).get("/api/hypotheses").json()
    assert items == [
        {
            "hypothesis_id": "H-L000001",
            "status": "adversarial_rejected",
            "adversarial": {"verdict": "rejected"},
        }
    ]


@pytest.mark.parametrize(
    "path",
    [
        "/api/loops/..%2Fsecrets",
        "/api/loops/latest",
        "/api/experiments/proto-xyz",
        "/api/experiments/..%2F..%2Fx",
    ],
)
def test_path_parameters_are_validated(lake: Lake, path: str) -> None:
    assert _client(lake).get(path).status_code in (400, 404)


def test_unknown_but_well_formed_ids_are_404(lake: Lake) -> None:
    client = _client(lake)
    assert client.get("/api/loops/20260101T000000").status_code == 404
    assert client.get("/api/experiments/proto-" + "0" * 24).status_code == 404


def test_kill_switch_is_reported(lake: Lake) -> None:
    lake.root.mkdir(parents=True, exist_ok=True)
    (lake.root / "KILL_SWITCH").write_text("engaged by test")
    client = _client(lake)
    assert client.get("/api/risk").json()["kill_switch_engaged"] is True
    assert client.get("/api/health").json()["kill_switch_engaged"] is True


def test_api_is_read_only(lake: Lake) -> None:
    api = create_app(_settings(lake))
    methods = {m for route in api.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}


def test_dashboard_page_is_served_without_html_injection(lake: Lake) -> None:
    response = _client(lake).get("/")
    assert response.status_code == 200
    assert "ORACLE" in response.text
    assert "innerHTML" not in response.text  # artifacts are rendered as text, never markup


def test_serve_refuses_non_loopback_host() -> None:
    result = CliRunner().invoke(cli_app, ["serve", "--host", "0.0.0.0"])  # noqa: S104
    assert result.exit_code == 2
    assert "loopback" in result.output
