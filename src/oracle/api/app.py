"""API and dashboard.

The research endpoints in this module only READ the lake and research artifacts. There is no
endpoint that ingests, runs research, changes a strategy or touches execution. Every number
shown there comes from a stored artifact: datasets and quality reports, loop reports,
discovery results, hypotheses, adversarial reports and the journal.

The banking and AI endpoints (`fintech_routes.py`) may change sandbox ledger state. There is no
authentication, so the server must stay bound to the loopback interface (`oracle serve`
enforces that), and two request guards apply to every route:
- the Host header must be a loopback name, which defeats DNS rebinding;
- any state-changing method needs the `X-Oracle-Client` header, which cross-site pages cannot
  send without a CORS preflight this server never grants.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from oracle import __version__
from oracle.api.fintech_routes import Clock, fintech_routers
from oracle.common.config import LOOPBACK, Settings
from oracle.data.lake import Lake
from oracle.data.providers.registry import provider_statuses
from oracle.data.query import connect
from oracle.fintech.ledger import LedgerError
from oracle.fintech.money import CurrencyError
from oracle.fintech.service import NotFoundError
from oracle.quality.checks import run_quality
from oracle.risk.engine import RiskConfig
from oracle.schemas.market import DatasetKind
from oracle.world_model.snapshot import WorldSnapshot, render_text

LOOP_NAME = re.compile(r"^\d{8}T\d{6}$")
PROTOCOL = re.compile(r"^proto-[0-9a-f]{24}$")
SYMBOL = r"^[A-Z0-9.\-]{1,15}$"
INTERVAL = r"^\d{1,3}[mhd]$"


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _latest_by_id(path: Path, key: str) -> list[dict[str, Any]]:
    """Append-only JSONL history; the last line per id is the current state."""
    if not path.exists():
        return []
    latest: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            item = json.loads(line)
            latest[str(item[key])] = item
    return list(latest.values())


def _dataset_summary(lake: Lake, kind: DatasetKind, quality: bool) -> dict[str, Any]:
    manifest = lake.head(kind)
    if manifest is None:
        return {"dataset": kind.value, "present": False}
    item: dict[str, Any] = {
        "dataset": kind.value,
        "present": True,
        "version": manifest.version,
        "rows": manifest.row_count,
        "partitions": len(manifest.partitions),
        "created_at": manifest.created_at.isoformat(),
        "integrity_problems": lake.verify(manifest),
    }
    if quality:
        item["quality"] = run_quality(lake, manifest, now=datetime.now(UTC)).model_dump(mode="json")
    return item


CLIENT_HEADER = "x-oracle-client"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# "testserver" is the Host header Starlette's TestClient sends.
ALLOWED_HOSTS = LOOPBACK | {"testserver"}


def _host(value: str) -> str:
    return (urlsplit(f"//{value}").hostname or "").lower()


class RequestGuard(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = _host(request.headers.get("host", ""))
        if host not in ALLOWED_HOSTS:
            return JSONResponse({"detail": "host not allowed"}, status_code=421)
        if request.method not in SAFE_METHODS:
            if not request.headers.get(CLIENT_HEADER):
                return JSONResponse({"detail": f"missing {CLIENT_HEADER} header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and _host(urlsplit(origin).netloc) != host:
                return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            # Revalidate (ETag) on every load so an upgraded dashboard is never served stale.
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    settings: Settings, *, kill_switch_path: Path | None = None, clock: Clock | None = None
) -> FastAPI:
    lake = Lake(settings.data_root)
    research = lake.resolve("research")
    loops = lake.resolve("loop")
    kill_path = kill_switch_path or lake.resolve("KILL_SWITCH")
    app = FastAPI(title="ORACLE", version=__version__, docs_url=None, redoc_url=None)
    app.add_middleware(RequestGuard)
    for router in fintech_routers(settings, clock):
        app.include_router(router)
    app.mount("/static", StaticFiles(packages=[("oracle.api", "static")]), name="static")

    @app.exception_handler(NotFoundError)
    def _not_found(_request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(LedgerError)
    @app.exception_handler(CurrencyError)
    def _rejected(_request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def loop_files() -> list[Path]:
        return sorted(loops.glob("*.json")) if loops.exists() else []

    def result_files(pattern: str = "*.json") -> list[Path]:
        root = research / "results"
        return sorted(root.glob(pattern)) if root.exists() else []

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        page = resources.files("oracle.api").joinpath("static/index.html")
        return page.read_text(encoding="utf-8")

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        providers = [
            {
                "name": s.capabilities.name,
                "enabled": s.enabled,
                "reason": s.reason,
                "requires_payment": s.capabilities.requires_payment,
                "synthetic": s.capabilities.synthetic,
            }
            for s in provider_statuses(settings)
        ]
        return {
            "version": __version__,
            "environment": settings.environment.value,
            "live_trading_enabled": settings.live_trading_enabled,
            "live_broker_adapter": "none (live orders always refuse)",
            "kill_switch_engaged": kill_path.exists(),
            "database_configured": settings.database_url is not None,
            "providers": providers,
            "server_time": datetime.now(UTC).isoformat(),
        }

    @app.get("/api/data")
    def data(quality: bool = False) -> list[dict[str, Any]]:
        return [_dataset_summary(lake, kind, quality) for kind in DatasetKind]

    @app.get("/api/symbols")
    def symbols() -> list[dict[str, Any]]:
        """Symbols and intervals in the current bars version, with their coverage."""
        manifest = lake.head(DatasetKind.BARS)
        if manifest is None:
            return []
        con = connect(lake, [manifest], verify=False)
        try:
            rows = con.execute(
                # epoch_ms keeps timestamps as integers (DuckDB tz-aware -> Python needs pytz).
                "SELECT symbol, interval, count(*), epoch_ms(min(event_ts)), "
                "epoch_ms(max(event_ts)), bool_or(is_synthetic) "
                "FROM bars GROUP BY 1, 2 ORDER BY 1, 2"
            ).fetchall()
        finally:
            con.close()
        return [
            {
                "symbol": s,
                "interval": i,
                "bars": n,
                "first": datetime.fromtimestamp(first / 1000, UTC).isoformat(),
                "last": datetime.fromtimestamp(last / 1000, UTC).isoformat(),
                "synthetic": synthetic,
            }
            for s, i, n, first, last, synthetic in rows
        ]

    @app.get("/api/bars")
    def bars(
        symbol: str = Query(pattern=SYMBOL),
        interval: str = Query(pattern=INTERVAL),
        limit: int = Query(default=1000, ge=10, le=5000),
    ) -> dict[str, Any]:
        """The most recent `limit` bars, oldest first, as [epoch_ms, o, h, l, c, v] rows."""
        manifest = lake.head(DatasetKind.BARS)
        if manifest is None:
            raise HTTPException(status_code=404, detail="no bars dataset yet")
        con = connect(lake, [manifest], verify=False)
        try:
            rows = con.execute(
                "SELECT epoch_ms(event_ts), open, high, low, close, volume, is_synthetic FROM ("
                "  SELECT * FROM bars WHERE symbol = ? AND interval = ?"
                "  ORDER BY event_ts DESC LIMIT ?) ORDER BY 1",
                [symbol, interval, limit],
            ).fetchall()
        finally:
            con.close()
        if not rows:
            raise HTTPException(status_code=404, detail="no bars for that symbol and interval")
        return {
            "symbol": symbol,
            "interval": interval,
            "dataset_version": manifest.version,
            "synthetic": any(r[6] for r in rows),
            "bars": [list(r[:6]) for r in rows],
        }

    @app.get("/api/loops")
    def loops_list() -> list[dict[str, Any]]:
        keys = (
            "started_at",
            "experiments",
            "tests_run",
            "validated_hypotheses",
            "adversarial_survivors",
            "notes",
        )
        return [
            {"name": p.stem, **{k: _read_json(p)[k] for k in keys}} for p in reversed(loop_files())
        ]

    @app.get("/api/loops/{name}")
    def loop_detail(name: str) -> Any:
        if not LOOP_NAME.fullmatch(name):
            raise HTTPException(status_code=400, detail="invalid loop report name")
        path = loops / f"{name}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="loop report not found")
        return _read_json(path)

    @app.get("/api/world")
    def world() -> dict[str, Any]:
        files = loop_files()
        if not files:
            raise HTTPException(status_code=404, detail="no loop report yet: oracle loop run-once")
        snapshot = WorldSnapshot.model_validate(_read_json(files[-1])["world"])
        return {"snapshot": snapshot.model_dump(mode="json"), "text": render_text(snapshot)}

    @app.get("/api/experiments")
    def experiments() -> list[dict[str, Any]]:
        out = []
        for path in result_files():
            r = _read_json(path)
            out.append(
                {
                    "protocol_hash": r["protocol_hash"],
                    "name": r["protocol"]["name"],
                    "horizons": r["protocol"]["horizons"],
                    "dataset_version": r["dataset_version"],
                    "n_tests": r["n_tests"],
                    "discovery_survivors": len(r["discovery_survivors"]),
                    "validated": len(r["validated"]),
                }
            )
        return out

    @app.get("/api/experiments/{protocol_hash}")
    def experiment_detail(
        protocol_hash: str, limit: int = Query(default=50, ge=1, le=1000)
    ) -> dict[str, Any]:
        if not PROTOCOL.fullmatch(protocol_hash):
            raise HTTPException(status_code=400, detail="invalid protocol hash")
        matches = result_files(f"{protocol_hash}__*.json")
        if not matches:
            raise HTTPException(status_code=404, detail="experiment not found")
        r = _read_json(matches[-1])
        # NaN p-values (zero-variance tests) are stored as null; list them last.
        tested = sorted(
            (t for t in r["tests"] if t["status"] == "tested"),
            key=lambda t: (t["p_value"] is None, t["p_value"] or 0.0),
        )
        return {
            "protocol": r["protocol"],
            "n_tests": r["n_tests"],
            "insufficient_events": sum(1 for t in r["tests"] if t["status"] != "tested"),
            "p_adjusted": r["p_adjusted"],
            "top_tests": tested[:limit],
            "validation_tests": r["validation_tests"],
            "validated": r["validated"],
        }

    @app.get("/api/hypotheses")
    def hypotheses() -> list[dict[str, Any]]:
        items = _latest_by_id(research / "hypotheses.jsonl", "hypothesis_id")
        for h in items:
            report = research / "adversarial" / f"{h['hypothesis_id']}.json"
            h["adversarial"] = _read_json(report) if report.exists() else None
        return sorted(items, key=lambda h: str(h["hypothesis_id"]))

    @app.get("/api/strategies")
    def strategies() -> list[dict[str, Any]]:
        files = loop_files()
        return list(_read_json(files[-1])["strategies"]) if files else []

    @app.get("/api/risk")
    def risk() -> dict[str, Any]:
        return {
            "limits": RiskConfig().model_dump(mode="json"),
            "kill_switch_engaged": kill_path.exists(),
            "live_trading_enabled": settings.live_trading_enabled,
        }

    @app.get("/api/journal")
    def journal(
        q: str = Query(default="", max_length=200), limit: int = Query(default=50, ge=1, le=500)
    ) -> list[dict[str, Any]]:
        path = research / "journal.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        entries = [json.loads(line) for line in lines if line.strip()]
        words = q.lower().split()
        if words:
            entries = [e for e in entries if all(w in json.dumps(e).lower() for w in words)]
        return list(reversed(entries))[:limit]

    return app
