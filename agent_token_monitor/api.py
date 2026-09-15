from __future__ import annotations

import json
import os
import atexit
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from agent_token_monitor.collector import Collector
from agent_token_monitor.config import alert_thresholds, collector_paths, load_config, privacy_options, section
from agent_token_monitor.storage import SQLiteStore


def _today_start() -> str:
    local_now = datetime.now().astimezone()
    return local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def _codex_session_titles() -> dict[str, str]:
    """Read Codex's local human-readable thread names without persisting log content."""
    explicit = os.environ.get("CODEX_SESSION_INDEX_PATH")
    if explicit:
        candidates = [Path(explicit).expanduser()]
    else:
        candidates = []
        log_path = os.environ.get("CODEX_LOG_PATH")
        if log_path:
            path = Path(log_path).expanduser()
            root = path if path.is_dir() else path.parent
            candidates.extend([root / "session_index.jsonl", root.parent / "session_index.jsonl"])
        codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        candidates.append(codex_home / "session_index.jsonl")
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        titles: dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    session_id = str(item.get("id") or item.get("session_id") or "").strip()
                    title = str(item.get("thread_name") or item.get("title") or item.get("name") or "").strip()
                    if session_id and title:
                        titles[session_id] = title
            return titles
        except OSError:
            continue
    return {}


_CLAUDE_TITLE_CACHE: dict[str, tuple[int, dict[str, str]]] = {}


def _claude_session_titles() -> dict[str, str]:
    """Read Claude Code's local custom/AI session titles, re-reading only changed files."""
    explicit = os.environ.get("CLAUDE_LOG_PATH")
    if explicit:
        root = Path(explicit).expanduser()
        files = [root] if root.is_file() else sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    else:
        root = Path(os.environ.get("CLAUDE_HOME", str(Path.home() / ".claude"))).expanduser() / "projects"
        files = sorted(root.rglob("*.jsonl")) if root.is_dir() else []
    current_paths = {str(path) for path in files}
    for path_string in list(_CLAUDE_TITLE_CACHE):
        if path_string not in current_paths:
            del _CLAUDE_TITLE_CACHE[path_string]
    titles: dict[str, str] = {}
    for path in files:
        path_string = str(path)
        try:
            mtime_ns = path.stat().st_mtime_ns
        except OSError:
            continue
        cached = _CLAUDE_TITLE_CACHE.get(path_string)
        if cached and cached[0] == mtime_ns:
            titles.update(cached[1])
            continue
        file_titles: dict[str, str] = {}
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if '"custom-title"' not in line and '"ai-title"' not in line:
                        continue
                    try:
                        item = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    session_id = str(item.get("sessionId") or item.get("session_id") or "").strip()
                    title = str(item.get("customTitle") or item.get("aiTitle") or "").strip()
                    if session_id and title:
                        file_titles[session_id] = title
        except (OSError, UnicodeError):
            continue
        _CLAUDE_TITLE_CACHE[path_string] = (mtime_ns, file_titles)
        titles.update(file_titles)
    return titles


def _attach_session_titles(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    codex_titles = _codex_session_titles()
    claude_titles = _claude_session_titles()
    if not codex_titles and not claude_titles:
        return rows
    for row in rows:
        session_id = str(row.get("external_session_id") or "")
        if row.get("provider") == "openai" or row.get("agent_name") == "Codex":
            title = codex_titles.get(session_id)
        elif row.get("provider") == "anthropic" or row.get("agent_name") == "Claude Code":
            title = claude_titles.get(session_id)
        else:
            title = None
        if title:
            row["session_name"] = title
    return rows


def create_app(db_path: str | Path | None = None) -> FastAPI:
    runtime_config = load_config()
    config_database = section(runtime_config, "database")
    config_pricing = section(runtime_config, "pricing")
    resolved_db = Path(db_path or os.environ.get("TOKEN_MONITOR_DB") or config_database.get("path") or "./data/token-monitor.db")
    pricing_path = os.environ.get("TOKEN_MONITOR_PRICING_PATH") or config_pricing.get("path")
    store = SQLiteStore(resolved_db, pricing_path=pricing_path)
    auto_scan_stop = threading.Event()
    auto_scan_thread: threading.Thread | None = None

    def auto_scan() -> None:
        monitoring = section(runtime_config, "monitoring")
        interval = max(1, int(os.environ.get("TOKEN_MONITOR_POLLING_INTERVAL", monitoring.get("polling_interval", 5))))
        worker_store = SQLiteStore(resolved_db, pricing_path=pricing_path)
        try:
            collector = Collector(worker_store, alert_thresholds=alert_thresholds(runtime_config))
            claude_paths = [os.path.expanduser(item) for item in collector_paths(runtime_config, "claude", ["~/.claude/projects"])]
            codex_paths = [os.path.expanduser(item) for item in collector_paths(runtime_config, "codex", ["~/.codex/sessions"])]
            if os.environ.get("CLAUDE_LOG_PATH"):
                claude_paths = [os.path.expanduser(os.environ["CLAUDE_LOG_PATH"])]
            if os.environ.get("CODEX_LOG_PATH"):
                codex_paths = [os.path.expanduser(os.environ["CODEX_LOG_PATH"])]
            privacy = privacy_options(runtime_config)
            while not auto_scan_stop.is_set():
                try:
                    collector.scan([*claude_paths, *codex_paths], **privacy)
                except Exception:
                    # A malformed provider event must not take down the local API.
                    import logging
                    logging.getLogger(__name__).exception("background collector scan failed")
                auto_scan_stop.wait(interval)
        finally:
            worker_store.close()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        nonlocal auto_scan_thread
        if os.environ.get("TOKEN_MONITOR_AUTOSCAN", "0").lower() in {"1", "true", "yes"}:
            auto_scan_thread = threading.Thread(target=auto_scan, name="token-monitor-collector", daemon=True)
            auto_scan_thread.start()
        yield
        auto_scan_stop.set()
        if auto_scan_thread is not None:
            auto_scan_thread.join(timeout=10)
        store.close()

    app = FastAPI(title="TokenPaw · AI Agent Token Observatory", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    cors_origins = [item.strip() for item in os.environ.get(
        "TOKEN_MONITOR_CORS_ORIGINS",
        "http://127.0.0.1:1420,http://localhost:1420,http://tauri.localhost,tauri://localhost,https://tauri.localhost",
    ).split(",") if item.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=cors_origins,
                       allow_methods=["GET", "POST"], allow_headers=["*"])

    @app.get("/api/overview")
    def overview(start: str | None = None, end: str | None = None,
                 agent: str | None = None, provider: str | None = None,
                 project: str | None = None, model: str | None = None,
                 session_id: str | None = None,
                 git_repository: str | None = None, git_branch: str | None = None,
                 alert_type: str | None = None,
                 min_tokens: int | None = Query(default=None, ge=0),
                 max_tokens: int | None = Query(default=None, ge=0),
                 min_cost: float | None = Query(default=None, ge=0),
                 max_cost: float | None = Query(default=None, ge=0)) -> dict[str, Any]:
        summary = store.summary()
        period_start = start or _today_start()
        period_rows = store.usage_timeline(1440, start=period_start, end=end, agent_name=agent,
                                           provider=provider, project=project, model=model,
                                           session_id=session_id,
                                           git_repository=git_repository, git_branch=git_branch,
                                           min_tokens=min_tokens, max_tokens=max_tokens,
                                           min_cost=min_cost, max_cost=max_cost, alert_type=alert_type)
        period = {key: sum(row[key] for row in period_rows) for key in
                 ("input_tokens", "effective_input_tokens", "cached_tokens", "fresh_tokens", "output_tokens", "reasoning_tokens", "actual_turns", "estimated_turns")}
        period["total_tokens"] = period["effective_input_tokens"] + period["output_tokens"]
        period["usage_quality"] = "Mixed" if period["actual_turns"] and period["estimated_turns"] else "Estimated" if period["estimated_turns"] else "Actual"
        period["has_estimates"] = bool(period["estimated_turns"])
        cost_row = store.connection.execute(
            """SELECT SUM(t.estimated_cost) AS estimated_cost FROM turns t
               JOIN sessions s ON s.id=t.session_id JOIN agents a ON a.id=s.agent_id
               LEFT JOIN projects p ON p.id=s.project_id
               WHERE datetime(t.timestamp) >= datetime(?)
                 AND (? IS NULL OR datetime(t.timestamp) <= datetime(?))
                 AND (? IS NULL OR a.name = ?) AND (? IS NULL OR a.provider = ?)
                 AND (? IS NULL OR p.project_name = ? OR p.project_path = ?)
                 AND (? IS NULL OR t.model = ?)
                 AND (? IS NULL OR s.id = ?)
                 AND (? IS NULL OR p.git_repository = ?)
                 AND (? IS NULL OR p.git_branch = ?)
                 AND (? IS NULL OR EXISTS (SELECT 1 FROM alerts al WHERE al.session_id=s.id AND al.type = ?))
                 AND (? IS NULL OR (CASE WHEN t.cached_input_tokens IS NOT NULL OR t.fresh_input_tokens IS NOT NULL
                     THEN COALESCE(t.cached_input_tokens,0) + COALESCE(t.fresh_input_tokens,0) ELSE COALESCE(t.input_tokens,0) END) >= ?)
                 AND (? IS NULL OR (CASE WHEN t.cached_input_tokens IS NOT NULL OR t.fresh_input_tokens IS NOT NULL
                     THEN COALESCE(t.cached_input_tokens,0) + COALESCE(t.fresh_input_tokens,0) ELSE COALESCE(t.input_tokens,0) END) <= ?)
                 AND (? IS NULL OR t.estimated_cost >= ?)
                 AND (? IS NULL OR t.estimated_cost <= ?)""",
            (period_start, end, end, agent, agent, provider, provider,
             project, project, project, model, model, session_id, session_id, git_repository, git_repository, git_branch, git_branch, alert_type, alert_type,
             min_tokens, min_tokens, max_tokens, max_tokens, min_cost, min_cost, max_cost, max_cost),
        ).fetchone()
        period["estimated_cost"] = cost_row["estimated_cost"]
        period_agents: dict[str, dict[str, Any]] = {}
        for bucket in period_rows:
            for agent_name, usage in bucket.get("agents", {}).items():
                item = period_agents.setdefault(agent_name, {"name": agent_name, "input_tokens": 0,
                    "effective_input_tokens": 0, "cached_tokens": 0, "fresh_tokens": 0, "output_tokens": 0,
                    "reasoning_tokens": 0, "estimated_cost": None, "actual_turns": 0, "estimated_turns": 0})
                for key in ("input_tokens", "effective_input_tokens", "cached_tokens", "fresh_tokens", "output_tokens",
                            "reasoning_tokens", "actual_turns", "estimated_turns"):
                    item[key] += usage.get(key) or 0
                if usage.get("estimated_cost") is not None:
                    item["estimated_cost"] = (item["estimated_cost"] or 0) + usage["estimated_cost"]
        for item in period_agents.values():
            denominator = item["cached_tokens"] + item["fresh_tokens"]
            item["total_tokens"] = item["effective_input_tokens"] + item["output_tokens"]
            item["cache_hit_rate"] = item["cached_tokens"] / denominator if denominator else None
            item["usage_quality"] = "Mixed" if item["actual_turns"] and item["estimated_turns"] else "Estimated" if item["estimated_turns"] else "Actual"
            item["has_estimates"] = bool(item["estimated_turns"])
        period["by_agent"] = sorted(period_agents.values(), key=lambda item: item["effective_input_tokens"] + item["output_tokens"], reverse=True)
        period_denominator = period["cached_tokens"] + period["fresh_tokens"]
        sessions = _attach_session_titles(store.sessions(start=period_start, end=end))
        if agent:
            sessions = [item for item in sessions if item["agent_name"] == agent]
        if provider:
            sessions = [item for item in sessions if item["provider"] == provider]
        if project:
            sessions = [item for item in sessions if item["project_name"] == project or item["project_path"] == project]
        if model:
            sessions = [item for item in sessions if item["model"] == model]
        if session_id:
            sessions = [item for item in sessions if item["id"] == session_id]
        if git_repository:
            sessions = [item for item in sessions if item["git_repository"] == git_repository]
        if git_branch:
            sessions = [item for item in sessions if item["git_branch"] == git_branch]
        if alert_type:
            alert_session_ids = {row["session_id"] for row in store.alerts(alert_type=alert_type)}
            sessions = [item for item in sessions if item["id"] in alert_session_ids]
        if min_tokens is not None:
            sessions = [item for item in sessions if (item["total_cached_input_tokens"] + item["total_fresh_input_tokens"] or item["total_input_tokens"]) >= min_tokens]
        if max_tokens is not None:
            sessions = [item for item in sessions if (item["total_cached_input_tokens"] + item["total_fresh_input_tokens"] or item["total_input_tokens"]) <= max_tokens]
        if min_cost is not None:
            sessions = [item for item in sessions if item["estimated_cost"] is not None and item["estimated_cost"] >= min_cost]
        if max_cost is not None:
            sessions = [item for item in sessions if item["estimated_cost"] is not None and item["estimated_cost"] <= max_cost]
        alerts = store.alerts(resolved=False, alert_type=alert_type, agent=agent, start=period_start, end=end)
        allowed_session_ids = {item["id"] for item in sessions}
        if session_id or alert_type or project or model or git_repository or git_branch or min_tokens is not None or max_tokens is not None or min_cost is not None or max_cost is not None:
            alerts = [item for item in alerts if item.get("session_id") in allowed_session_ids]
        project_totals: dict[str, dict[str, Any]] = {}
        for session in sessions:
            project_key = session.get("project_id") or f"unknown:{session.get('project_name') or 'Unknown project'}"
            item = project_totals.setdefault(project_key, {
                "id": session.get("project_id") or project_key,
                "project_name": session.get("project_name") or "Unknown project",
                "project_path": session.get("project_path"),
                "agent_name": session.get("agent_name"),
                "input_tokens": 0, "cached_tokens": 0, "fresh_tokens": 0,
                "output_tokens": 0, "reasoning_tokens": 0, "estimated_cost": None,
                "actual_turns": 0, "estimated_turns": 0, "session_count": 0,
            })
            item["input_tokens"] += session.get("total_input_tokens") or 0
            item["cached_tokens"] += session.get("total_cached_input_tokens") or 0
            item["fresh_tokens"] += session.get("total_fresh_input_tokens") or 0
            item["output_tokens"] += session.get("total_output_tokens") or 0
            item["reasoning_tokens"] += session.get("total_reasoning_tokens") or 0
            item["actual_turns"] += session.get("actual_turns") or 0
            item["estimated_turns"] += session.get("estimated_turns") or 0
            item["session_count"] += 1
            if session.get("estimated_cost") is not None:
                item["estimated_cost"] = (item["estimated_cost"] or 0) + session["estimated_cost"]
        top_projects = []
        for item in project_totals.values():
            item["effective_input_tokens"] = item["cached_tokens"] + item["fresh_tokens"] or item["input_tokens"]
            item["total_tokens"] = item["effective_input_tokens"] + item["output_tokens"]
            denominator = item["cached_tokens"] + item["fresh_tokens"]
            item["cache_hit_rate"] = item["cached_tokens"] / denominator if denominator else None
            item["usage_quality"] = "Mixed" if item["actual_turns"] and item["estimated_turns"] else "Estimated" if item["estimated_turns"] else "Actual"
            top_projects.append(item)
        top_projects.sort(key=lambda item: item["total_tokens"], reverse=True)
        agent_rows = store.agents(provider=provider)
        return {"today": {**period, "cache_hit_rate": period["cached_tokens"] / period_denominator if period_denominator else None},
                "all_time": summary, "agents": agent_rows,
                "active_sessions": [item for item in sessions if item["status"] == "ACTIVE"],
                "top_sessions": sorted(sessions, key=lambda item: item["total_fresh_input_tokens"] + item["total_cached_input_tokens"] + item["total_output_tokens"], reverse=True)[:10],
                "top_projects": top_projects[:10],
                "alerts": {"total": len(alerts), "critical": sum(item.get("severity") == "CRITICAL" for item in alerts),
                           "warning": sum(item.get("severity") == "WARNING" for item in alerts)}}

    @app.post("/api/scan")
    def scan() -> dict[str, int]:
        claude_path = os.environ.get("CLAUDE_LOG_PATH")
        codex_path = os.environ.get("CODEX_LOG_PATH")
        claude_paths = [os.path.expanduser(claude_path)] if claude_path else [os.path.expanduser(item) for item in collector_paths(runtime_config, "claude", ["~/.claude/projects"])]
        codex_paths = [os.path.expanduser(codex_path)] if codex_path else [os.path.expanduser(item) for item in collector_paths(runtime_config, "codex", ["~/.codex/sessions"])]
        return Collector(store, alert_thresholds=alert_thresholds(runtime_config)).scan(
            [*claude_paths, *codex_paths], **privacy_options(runtime_config))

    @app.get("/api/agents")
    def agents(provider: str | None = None, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return store.agents(provider=provider, start=start, end=end)

    @app.get("/api/projects")
    def projects(agent: str | None = None, provider: str | None = None,
                 model: str | None = None, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return store.projects(agent=agent, provider=provider, model=model, start=start, end=end)

    @app.get("/api/sessions")
    def sessions(agent: str | None = None, provider: str | None = None, project: str | None = None,
                 model: str | None = None, git_repository: str | None = None, git_branch: str | None = None,
                 session_id: str | None = None,
                 status: str | None = None, start: str | None = None, end: str | None = None,
                 alert_type: str | None = None, min_tokens: int | None = Query(default=None, ge=0),
                 max_tokens: int | None = Query(default=None, ge=0), min_cost: float | None = Query(default=None, ge=0),
                 max_cost: float | None = Query(default=None, ge=0)) -> list[dict[str, Any]]:
        result = _attach_session_titles(store.sessions(start=start, end=end))
        if agent:
            result = [item for item in result if item["agent_name"] == agent]
        if provider:
            result = [item for item in result if item["provider"] == provider]
        if project:
            result = [item for item in result if item["project_name"] == project or item["project_path"] == project]
        if model:
            result = [item for item in result if item["model"] == model]
        if session_id:
            result = [item for item in result if item["id"] == session_id]
        if git_repository:
            result = [item for item in result if item["git_repository"] == git_repository]
        if git_branch:
            result = [item for item in result if item["git_branch"] == git_branch]
        if status:
            result = [item for item in result if item["status"] == status]
        if alert_type:
            session_ids = {row["session_id"] for row in store.alerts() if row["type"] == alert_type}
            result = [item for item in result if item["id"] in session_ids]
        if min_tokens is not None:
            result = [item for item in result if (item["total_cached_input_tokens"] + item["total_fresh_input_tokens"] or item["total_input_tokens"]) >= min_tokens]
        if max_tokens is not None:
            result = [item for item in result if (item["total_cached_input_tokens"] + item["total_fresh_input_tokens"] or item["total_input_tokens"]) <= max_tokens]
        if min_cost is not None:
            result = [item for item in result if item["estimated_cost"] is not None and item["estimated_cost"] >= min_cost]
        if max_cost is not None:
            result = [item for item in result if item["estimated_cost"] is not None and item["estimated_cost"] <= max_cost]
        return result

    @app.get("/api/sessions/{session_id}")
    def session(session_id: str) -> dict[str, Any]:
        result = store.session(session_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Session not found")
        _attach_session_titles([result])
        return result

    @app.get("/api/sessions/{session_id}/turns")
    def session_turns(session_id: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        if store.session(session_id) is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return store.turns(session_id, start=start, end=end)

    @app.get("/api/sessions/{session_id}/timeline")
    def session_timeline(session_id: str, start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        result = store.session_timeline(session_id, start=start, end=end)
        if result is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return result

    @app.get("/api/turns/{turn_id}")
    def turn(turn_id: str) -> dict[str, Any]:
        result = store.turn(turn_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Turn not found")
        return result

    @app.get("/api/turns/{turn_id}/impact")
    def turn_impact(turn_id: str) -> dict[str, Any]:
        result = store.turn_impact(turn_id)
        if result is None:
            raise HTTPException(status_code=404, detail="Turn not found")
        return result

    @app.get("/api/sessions/{session_id}/insights")
    def session_insights(session_id: str, start: str | None = None, end: str | None = None) -> dict[str, Any]:
        result = store.session_insights(session_id, start=start, end=end)
        if result is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return result

    @app.get("/api/alerts")
    def alerts(resolved: bool | None = None, alert_type: str | None = None,
               severity: str | None = None, agent: str | None = None,
               start: str | None = None, end: str | None = None) -> list[dict[str, Any]]:
        return store.alerts(resolved=resolved, alert_type=alert_type, severity=severity,
                            agent=agent, start=start, end=end)

    @app.get("/api/search")
    def search(q: str = Query(min_length=1, max_length=200),
               limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        return store.search(q, limit)

    @app.get("/api/usage/timeline")
    def timeline(bucket_minutes: int = Query(default=60, description="One of 5, 15, 60, 1440"),
                 start: str | None = None, end: str | None = None, agent: str | None = None,
                 provider: str | None = None, project: str | None = None, model: str | None = None,
                 session_id: str | None = None,
                 git_repository: str | None = None, git_branch: str | None = None,
                 alert_type: str | None = None,
                 min_tokens: int | None = Query(default=None, ge=0), max_tokens: int | None = Query(default=None, ge=0),
                 min_cost: float | None = Query(default=None, ge=0), max_cost: float | None = Query(default=None, ge=0)) -> list[dict[str, Any]]:
        return store.usage_timeline(bucket_minutes, start=start, end=end, agent_name=agent, provider=provider,
                                    project=project, model=model, session_id=session_id, git_repository=git_repository, git_branch=git_branch,
                                    min_tokens=min_tokens, max_tokens=max_tokens, min_cost=min_cost, max_cost=max_cost,
                                    alert_type=alert_type)

    @app.get("/api/usage/models")
    def usage_models(start: str | None = None, end: str | None = None,
                     agent: str | None = None, provider: str | None = None) -> list[dict[str, Any]]:
        return store.usage_by_model(start=start, end=end, agent=agent, provider=provider)

    @app.get("/api/usage/projects")
    def usage_projects(start: str | None = None, end: str | None = None,
                       agent: str | None = None, provider: str | None = None) -> list[dict[str, Any]]:
        return store.usage_by_project(start=start, end=end, agent=agent, provider=provider)

    frontend = Path(__file__).resolve().parent.parent / "frontend"
    if frontend.exists():
        app.mount("/", StaticFiles(directory=frontend, html=True), name="frontend")
    return app


app = create_app()
atexit.register(app.state.store.close)
