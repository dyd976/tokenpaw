from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterable

from agent_token_monitor.models import NormalizedEvent


class AgentAdapter(ABC):
    name: str
    provider: str

    @abstractmethod
    def can_parse(self, path: Path, sample: Iterable[dict[str, Any]]) -> bool:
        raise NotImplementedError

    @abstractmethod
    def detect_version(self, event: dict[str, Any]) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def parse_event(
        self,
        event: dict[str, Any],
        source_file: str,
        line_number: int,
        state: dict[str, Any],
    ) -> list[NormalizedEvent]:
        raise NotImplementedError

    def normalize(
        self,
        event: dict[str, Any],
        source_file: str,
        line_number: int,
        state: dict[str, Any],
    ) -> list[NormalizedEvent]:
        return self.parse_event(event, source_file, line_number, state)
