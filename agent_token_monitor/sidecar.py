"""Packaged desktop sidecar entry point for the local FastAPI service."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path

import uvicorn


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    port = int(os.environ.get("TOKEN_MONITOR_PORT", "0")) or _free_port()
    db_path = Path(os.environ.get("TOKEN_MONITOR_DB", "./data/token-monitor.db"))
    os.environ["TOKEN_MONITOR_DB"] = str(db_path)
    from agent_token_monitor.api import app

    print(json.dumps({"ready": True, "port": port}), flush=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
