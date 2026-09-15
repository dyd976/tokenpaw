from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from agent_token_monitor.collector import Collector
from agent_token_monitor.config import alert_thresholds, collector_paths, load_config, privacy_options, section
from agent_token_monitor.storage import SQLiteStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-token-monitor")
    parser.add_argument("command", choices=["scan", "status", "sessions", "alerts", "dashboard"])
    parser.add_argument("--config", default=os.environ.get("TOKEN_MONITOR_CONFIG"), help="Optional local YAML/JSON config")
    parser.add_argument("--db", default=None)
    parser.add_argument("--pricing", default=None, help="Local YAML/JSON pricing table")
    parser.add_argument("--claude-path", action="append", default=[])
    parser.add_argument("--codex-path", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    parser.add_argument("--limit", type=int, default=20, help="Maximum rows for sessions/alerts")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    return parser


def _compact(value: int | float | None) -> str:
    return "—" if value is None else f"{value:,.0f}"


def _effective_input(row: dict) -> int:
    cached = row.get("cached_tokens")
    fresh = row.get("fresh_tokens")
    if cached is None:
        cached = row.get("total_cached_input_tokens")
    if fresh is None:
        fresh = row.get("total_fresh_input_tokens")
    cached = cached or 0
    fresh = fresh or 0
    return cached + fresh or row.get("input_tokens", row.get("total_input_tokens")) or 0


def _today_start() -> str:
    now = datetime.now().astimezone()
    return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()


def _print_status(store: SQLiteStore, as_json: bool) -> None:
    today_start = _today_start()
    agents = store.agents(start=today_start)
    sessions = store.sessions(start=today_start)
    projects = store.projects(start=today_start)
    alerts = store.alerts(resolved=False, start=today_start)
    today = {
        "input_tokens": sum(item.get("input_tokens") or 0 for item in agents),
        "effective_input_tokens": sum(item.get("effective_input_tokens") or 0 for item in agents),
        "cached_tokens": sum(item.get("cached_tokens") or 0 for item in agents),
        "fresh_tokens": sum(item.get("fresh_tokens") or 0 for item in agents),
        "output_tokens": sum(item.get("output_tokens") or 0 for item in agents),
        "reasoning_tokens": sum(item.get("reasoning_tokens") or 0 for item in agents),
        "estimated_cost": sum(item.get("estimated_cost") or 0 for item in agents) if any(item.get("estimated_cost") is not None for item in agents) else None,
    }
    denominator = today["cached_tokens"] + today["fresh_tokens"]
    today["cache_hit_rate"] = today["cached_tokens"] / denominator if denominator else None
    today["total_tokens"] = today["effective_input_tokens"] + today["output_tokens"]
    top_session = max(sessions, key=lambda item: _effective_input(item) + (item.get("total_output_tokens") or 0), default=None)
    top_project = max(projects, key=lambda item: (item.get("effective_input_tokens") or _effective_input(item)) + (item.get("output_tokens") or 0), default=None)
    payload = {"today": today, "agents": agents, "top_project": top_project, "top_session": top_session, "alerts": alerts,
               "all_time": store.summary()}
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    print("Today")
    for agent in agents:
        total = (agent.get("effective_input_tokens") or 0) + (agent.get("output_tokens") or 0)
        cache_rate = agent.get("cache_hit_rate")
        cache_label = f"{cache_rate:.1%}" if cache_rate is not None else "Unknown"
        print(f"{agent['name']}: {_compact(total)} tokens | cache {cache_label}")
    print(f"Cache Hit: {today['cache_hit_rate']:.1%}" if today["cache_hit_rate"] is not None else "Cache Hit: Unknown")
    print(f"Alerts: {len(alerts)}")
    if top_session:
        print(f"Top session: {top_session.get('project_name') or 'Unknown project'} | {_compact(_effective_input(top_session) + (top_session.get('total_output_tokens') or 0))} tokens")
    if top_project:
        print(f"Top project: {top_project.get('project_name') or 'Unknown project'} | {_compact((top_project.get('effective_input_tokens') or _effective_input(top_project)) + (top_project.get('output_tokens') or 0))} tokens")


def _print_sessions(store: SQLiteStore, limit: int, as_json: bool) -> None:
    rows = sorted(store.sessions(), key=lambda item: _effective_input(item) + (item.get("total_output_tokens") or 0), reverse=True)[:max(1, limit)]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    print("Sessions")
    for row in rows:
        total = _effective_input(row) + (row.get("total_output_tokens") or 0)
        health = row.get("health_status") or row.get("status") or "UNKNOWN"
        print(f"{row.get('agent_name') or 'Unknown'} | {row.get('project_name') or 'Unknown project'} | {row.get('external_session_id', '')[:12]} | {_compact(total)} tokens | {health}")


def _print_alerts(store: SQLiteStore, limit: int, as_json: bool) -> None:
    rows = store.alerts(resolved=False)[:max(1, limit)]
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2, default=str))
        return
    print(f"Active alerts: {len(rows)}")
    for row in rows:
        cause = row.get("likely_cause") or {}
        target = cause.get("file_path") or cause.get("tool_name") or cause.get("kind") or "unknown cause"
        print(f"[{row.get('severity', 'UNKNOWN')}] {row.get('type', 'ALERT')} | {row.get('agent_name', 'Unknown')} | {row.get('project_name', 'Unknown project')} | {target}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        # Test streams and embedded callers may not expose reconfigure().
        pass
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)
    config = load_config(args.config)
    database = section(config, "database")
    pricing = args.pricing or section(config, "pricing").get("path")
    if pricing:
        os.environ["TOKEN_MONITOR_PRICING_PATH"] = str(Path(pricing).expanduser())
    db_path = args.db or database.get("path") or "./data/token-monitor.db"
    store = SQLiteStore(db_path)
    try:
        if args.command == "scan":
            claude_paths = args.claude_path or collector_paths(config, "claude", ["~/.claude/projects"])
            codex_paths = args.codex_path or collector_paths(config, "codex", ["~/.codex/sessions"])
            stats = Collector(store, alert_thresholds=alert_thresholds(config)).scan(
                [*claude_paths, *codex_paths], **privacy_options(config))
            print(json.dumps(stats, ensure_ascii=False, indent=2))
        elif args.command == "status":
            _print_status(store, args.json)
        elif args.command == "sessions":
            _print_sessions(store, args.limit, args.json)
        elif args.command == "alerts":
            _print_alerts(store, args.limit, args.json)
        elif args.command == "dashboard":
            import uvicorn
            os.environ["TOKEN_MONITOR_DB"] = str(Path(db_path).resolve())
            if args.config:
                os.environ["TOKEN_MONITOR_CONFIG"] = str(Path(args.config).expanduser().resolve())
            store.close()
            uvicorn.run("agent_token_monitor.api:app", host=args.host, port=args.port, reload=False)
            return 0
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
