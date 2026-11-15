"""``gplm dashboard``: a local, read-only browser dashboard (Python standard library only).

Binds to 127.0.0.1 by default. Every request opens the SQLite database read-only, so the
dashboard can run while ``gplm daily`` writes. JSON endpoints back the single-page UI in
``static/index.html``; ``/export/<table>.<csv|json>`` downloads any audit table.
"""
from __future__ import annotations

import json
import mimetypes
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import numpy as np

from . import CLASSIFICATION, __version__
from .db import AuditDB
from .report import accuracy_table, live_forecasts, trace_prediction, trace_run
from .settings import Settings

STATIC = Path(__file__).with_name("static")


def _db(s: Settings) -> AuditDB:
    return AuditDB(s.db_path, s.events_dir, readonly=True)


def summary(s: Settings) -> dict[str, Any]:
    db = _db(s)
    try:
        run = db.one("SELECT * FROM runs ORDER BY started_utc DESC LIMIT 1")
        daily = db.one("SELECT * FROM runs WHERE kind='daily' ORDER BY started_utc DESC LIMIT 1")
        fresh = db.q("SELECT symbol, last, first, rows, built_utc, summary_json FROM candle_cache ORDER BY symbol")
        for f in fresh:
            sj = json.loads(f.pop("summary_json") or "{}")
            f.update(minute_last=sj.get("minute_last"), minute_sessions=sj.get("minute_sessions_used"),
                     minute_derived_rows=sj.get("minute_derived_rows"))
        symbols = sorted({r["symbol"] for r in db.q("SELECT DISTINCT symbol FROM predictions")})
        acc = accuracy_table(db, symbols) if symbols else []
        best_models = sorted([a for a in acc if a["source_kind"] == "MODEL" and (a["evaluated"] or 0) >= 10],
                             key=lambda a: -(a.get("accuracy_minus_majority") or -9))[:10]
        strat = sorted([a for a in acc if a["source_kind"] != "MODEL" and (a["evaluated"] or 0) >= 3],
                       key=lambda a: -(a.get("all_signals_mean_net") or -9))[:10]
        warn = db.one("SELECT SUM(level='WARN') w, SUM(level='ERROR') e FROM events WHERE run_id=?",
                      (run["run_id"],)) if run else {"w": 0, "e": 0}
        api = db.one("SELECT COUNT(*) n, SUM(cost_usd) usd, SUM(cost_usd IS NULL) unknown, SUM(success=0) failed "
                     "FROM api_calls")
        forecasts = live_forecasts(db, symbols) if symbols else []
        dev = db.one("SELECT device, COUNT(*) n FROM training_history GROUP BY device ORDER BY n DESC LIMIT 1")
        totals = db.one("SELECT (SELECT COUNT(*) FROM predictions) predictions, (SELECT COUNT(*) FROM evaluations) "
                        "evaluations, (SELECT COUNT(*) FROM strategies) strategy_versions, "
                        "(SELECT COUNT(*) FROM data_issues WHERE severity IN ('WARN','ERROR')) data_warnings")
        progress = json.loads(s.progress_path.read_text(encoding="utf-8")) if s.progress_path.exists() else None
        return {"app_version": __version__, "classification": CLASSIFICATION, "latest_run": run, "latest_daily": daily,
                "freshness": fresh, "symbols": symbols, "best_models": best_models, "best_strategies": strat,
                "warnings": warn, "api": api, "forecasts": forecasts, "training_device": dev, "totals": totals,
                "progress": progress}
    finally:
        db.close()


def _rows(s: Settings, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    db = _db(s)
    try:
        return db.q(sql, params)
    finally:
        db.close()


def stock(s: Settings, symbol: str) -> dict[str, Any]:
    cache = _rows(s, "SELECT * FROM candle_cache WHERE symbol=?", (symbol,))
    candles: list[dict[str, Any]] = []
    if cache and Path(cache[0]["path"]).is_file():
        with np.load(cache[0]["path"], allow_pickle=False) as z:
            d, o = z["d_date"][-160:], z["d_ohlcv"][-160:]
            src = z["d_source"][-160:]
        candles = [{"date": str(a), "open": float(b[0]), "high": float(b[1]), "low": float(b[2]), "close": float(b[3]),
                    "volume": float(b[4]), "source": str(c)} for a, b, c in zip(d, o, src)]
    preds = _rows(s, "SELECT p.prediction_id, p.mode, p.timeframe, p.source_kind, p.source_id, p.horizon_bars, "
                     "p.decision_label, p.target_session, p.direction, p.prob_calibrated, p.prob_raw, p.actionable, "
                     "p.entry_ref, p.stop, p.target, e.realized_return, e.correct, e.trade_net_return, e.exit_reason "
                     "FROM predictions p LEFT JOIN evaluations e ON e.prediction_id=p.prediction_id WHERE p.symbol=? "
                     "ORDER BY p.decision_ns DESC LIMIT 400", (symbol,))
    issues = _rows(s, "SELECT timeframe, period, code, severity, detail_json FROM data_issues WHERE symbol=? "
                      "ORDER BY severity DESC, period DESC LIMIT 300", (symbol,))
    db = _db(s)
    try:
        acc = accuracy_table(db, [symbol])
    finally:
        db.close()
    return {"symbol": symbol, "candles": candles, "predictions": preds, "issues": issues, "accuracy": acc,
            "cache": cache[0] if cache else None}


def rolling_series(s: Settings, lane: str, source: str, horizon: int) -> list[dict[str, Any]]:
    rows = _rows(s, "SELECT p.decision_label, e.correct, e.brier, e.trade_net_return FROM predictions p JOIN evaluations e "
                    "ON e.prediction_id=p.prediction_id WHERE p.lane_key=? AND p.source_id=? AND p.horizon_bars=? AND "
                    "e.correct IS NOT NULL ORDER BY p.decision_ns", (lane, source, horizon))
    out, acc = [], []
    for r in rows:
        acc.append(r["correct"])
        win = acc[-20:]
        out.append({"t": r["decision_label"], "correct": r["correct"], "rolling20": sum(win) / len(win),
                    "cumulative": sum(acc) / len(acc), "brier": r["brier"]})
    return out


class Handler(BaseHTTPRequestHandler):
    settings: Settings

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        return

    def _send(self, body: bytes, ctype: str, status: int = 200, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, value: Any, status: int = 200) -> None:
        self._send(json.dumps(value, default=str).encode(), "application/json; charset=utf-8", status)

    def do_GET(self) -> None:  # noqa: N802
        s = self.settings
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        routes: dict[str, Callable[[], Any]] = {
            "/api/summary": lambda: summary(s),
            "/api/predictions": lambda: _rows(
                s, "SELECT p.prediction_id, p.mode, p.symbol, p.timeframe, p.source_kind, p.source_id, p.horizon_bars, "
                   "p.decision_label, p.target_session, p.direction, p.prob_calibrated, p.actionable, p.entry_ref, p.stop, "
                   "p.target, e.realized_return, e.correct, e.trade_net_return FROM predictions p LEFT JOIN evaluations e "
                   "ON e.prediction_id=p.prediction_id WHERE (?='' OR p.symbol=?) AND (?='' OR p.mode=?) AND "
                   "(?='' OR p.actionable=CAST(? AS INTEGER)) ORDER BY p.decision_ns DESC LIMIT ?",
                (q.get("symbol", ""), q.get("symbol", ""), q.get("mode", ""), q.get("mode", ""), q.get("actionable", ""),
                 q.get("actionable", "0"), int(q.get("limit", 300)))),
            "/api/strategies": lambda: _rows(s, "SELECT s.*, (SELECT COUNT(*) FROM predictions p WHERE p.source_id=s.strategy_id) "
                                                "signals FROM strategies s ORDER BY created_utc DESC LIMIT 1000"),
            "/api/strategy": lambda: {"versions": _rows(s, "SELECT * FROM strategies WHERE strategy_id=? ORDER BY version",
                                                        (q.get("id", ""),)),
                                      "document": _strategy_doc(s, q.get("id", "")),
                                      "outcomes": _rows(s, "SELECT p.decision_label, p.symbol, p.timeframe, p.mode, "
                                                           "p.actionable, e.trade_net_return, e.exit_reason FROM predictions p "
                                                           "LEFT JOIN evaluations e ON e.prediction_id=p.prediction_id WHERE "
                                                           "p.source_id=? ORDER BY p.decision_ns DESC LIMIT 300", (q.get("id", ""),))},
            "/api/training": lambda: _rows(s, "SELECT step_label, epoch, loss, train_acc, rows, device, seconds FROM "
                                              "training_history WHERE lane_key=? AND model_kind=? AND horizon=? ORDER BY id "
                                              "DESC LIMIT 3000", (q.get("lane", ""), q.get("kind", "mlp"), int(q.get("h", 1)))),
            "/api/rolling": lambda: rolling_series(s, q.get("lane", ""), q.get("source", "logit"), int(q.get("h", 1))),
            "/api/issues": lambda: _rows(s, "SELECT symbol, timeframe, period, code, severity, file, detail_json, first_seen_utc "
                                            "FROM data_issues WHERE (?='' OR symbol=?) ORDER BY id DESC LIMIT 1000",
                                         (q.get("symbol", ""), q.get("symbol", ""))),
            "/api/issue_counts": lambda: _rows(s, "SELECT code, severity, COUNT(*) n FROM data_issues GROUP BY code, severity "
                                                  "ORDER BY n DESC"),
            "/api/api_calls": lambda: _rows(s, "SELECT * FROM api_calls ORDER BY id DESC LIMIT 500"),
            "/api/runs": lambda: _rows(s, "SELECT run_id, kind, command, started_utc, finished_utc, status, device FROM runs "
                                          "ORDER BY started_utc DESC LIMIT 200"),
            "/api/events": lambda: _rows(s, "SELECT seq, ts_utc, run_id, kind, level, subject, substr(payload_json,1,600) payload "
                                            "FROM events WHERE (?='' OR run_id=?) AND (?='' OR kind=?) AND (?='' OR subject=?) "
                                            "ORDER BY seq DESC LIMIT ?",
                                         (q.get("run", ""), q.get("run", ""), q.get("kind", ""), q.get("kind", ""),
                                          q.get("subject", ""), q.get("subject", ""), int(q.get("limit", 300)))),
            "/api/stock": lambda: stock(s, q.get("symbol", "")),
            "/api/trace": lambda: _with_db(s, lambda db: trace_prediction(db, q.get("id", ""))),
            "/api/run": lambda: _with_db(s, lambda db: trace_run(db, q.get("id", ""))),
            "/api/progress": lambda: json.loads(s.progress_path.read_text(encoding="utf-8")) if s.progress_path.exists() else {},
        }
        try:
            if path in routes:
                return self._json(routes[path]())
            if path.startswith("/export/"):
                name = path[len("/export/"):]
                table, _, fmt = name.partition(".")
                db = _db(s)
                try:
                    body = db.export_table(table, "json" if fmt == "json" else "csv")
                finally:
                    db.close()
                return self._send(body.encode(), "application/json" if fmt == "json" else "text/csv; charset=utf-8",
                                  extra={"Content-Disposition": f"attachment; filename={table}.{fmt or 'csv'}"})
            if path.startswith("/reports/"):
                target = (s.reports_dir / path[len("/reports/"):]).resolve()
                if s.reports_dir.resolve() in target.parents and target.is_file():
                    ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                    return self._send(target.read_bytes(), ctype)
                return self._json({"error": "not found"}, 404)
            name = "index.html" if path == "/" else path.lstrip("/")
            target = (STATIC / name).resolve()
            if STATIC.resolve() in target.parents and target.is_file():
                ctype = mimetypes.guess_type(target.name)[0] or "text/html"
                return self._send(target.read_bytes(), ctype + ("; charset=utf-8" if ctype.startswith("text") else ""))
            return self._json({"error": "not found"}, 404)
        except FileNotFoundError as exc:
            return self._json({"error": str(exc), "hint": "run `gplm daily --stocks ...` first"}, HTTPStatus.SERVICE_UNAVAILABLE)
        except KeyError as exc:
            return self._json({"error": str(exc)}, 404)
        except Exception as exc:  # report, never crash the server
            return self._json({"error": f"{type(exc).__name__}: {exc}"}, 500)


def _with_db(s: Settings, fn: Callable[[AuditDB], Any]) -> Any:
    db = _db(s)
    try:
        return fn(db)
    finally:
        db.close()


def _strategy_doc(s: Settings, sid: str) -> Any:
    rows = _rows(s, "SELECT path FROM strategies WHERE strategy_id=? ORDER BY version DESC LIMIT 1", (sid,))
    if not rows:
        return None
    try:
        return json.loads(Path(rows[0]["path"]).read_text(encoding="utf-8"))
    except OSError as exc:
        return {"error": str(exc)}


def serve(s: Settings, host: str, port: int, open_browser: bool = False) -> None:
    handler = type("GPLMHandler", (Handler,), {"settings": s})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"GPLM dashboard on {url}  (read-only; Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser

        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
