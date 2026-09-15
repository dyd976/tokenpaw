from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_token_monitor.utils import parse_timestamp


@dataclass(frozen=True, slots=True)
class PricingRule:
    provider: str
    model: str
    input_price: float | None = None
    cached_input_price: float | None = None
    output_price: float | None = None
    reasoning_price: float | None = None
    effective_from: str | None = None
    effective_to: str | None = None

    def matches(self, provider: str | None, model: str | None, timestamp: str | None) -> bool:
        if self.provider not in {"*", provider or ""} or self.model not in {"*", model or ""}:
            return False
        value = parse_timestamp(timestamp) or datetime.now(timezone.utc)
        value = value.astimezone(timezone.utc)
        effective_from = parse_timestamp(self.effective_from) if self.effective_from else None
        effective_to = parse_timestamp(self.effective_to) if self.effective_to else None
        return (effective_from is None or value >= effective_from.astimezone(timezone.utc)) and \
            (effective_to is None or value < effective_to.astimezone(timezone.utc))


class PricingTable:
    """Local, optional model pricing. Prices are USD per one million tokens."""

    def __init__(self, rules: list[PricingRule] | None = None):
        self.rules = rules or []

    @classmethod
    def from_file(cls, path: str | Path | None = None) -> "PricingTable":
        raw_path = path or os.environ.get("TOKEN_MONITOR_PRICING_PATH")
        if not raw_path:
            return cls()
        file_path = Path(raw_path).expanduser()
        if not file_path.exists():
            return cls()
        try:
            text = file_path.read_text(encoding="utf-8")
            if file_path.suffix.lower() == ".json":
                payload = json.loads(text)
            else:
                try:
                    import yaml
                    payload = yaml.safe_load(text)
                except ImportError:
                    payload = json.loads(text)
            values = payload.get("pricing", payload) if isinstance(payload, dict) else payload
            if isinstance(values, dict):
                values = [dict(value, model=key) if isinstance(value, dict) else {"model": key} for key, value in values.items()]
            rules = [cls._rule(item) for item in values or [] if isinstance(item, dict)]
            return cls([rule for rule in rules if rule is not None])
        except (OSError, ValueError, TypeError):
            return cls()

    @staticmethod
    def _rule(item: dict[str, Any]) -> PricingRule | None:
        try:
            return PricingRule(
                provider=str(item.get("provider") or "*"), model=str(item.get("model") or "*"),
                input_price=_number(item.get("input_price")), cached_input_price=_number(item.get("cached_input_price")),
                output_price=_number(item.get("output_price")), reasoning_price=_number(item.get("reasoning_price")),
                effective_from=_text(item.get("effective_from")), effective_to=_text(item.get("effective_to")),
            )
        except (TypeError, ValueError):
            return None

    def estimate(self, *, provider: str | None, model: str | None, timestamp: str | None,
                 cached_tokens: int | None, fresh_tokens: int | None, input_tokens: int | None,
                 output_tokens: int | None, reasoning_tokens: int | None) -> float | None:
        rule = next((item for item in self.rules if item.matches(provider, model, timestamp)), None)
        if rule is None:
            return None
        cached = cached_tokens or 0
        fresh = fresh_tokens if fresh_tokens is not None else input_tokens
        fresh = fresh or 0
        output = output_tokens or 0
        reasoning = reasoning_tokens or 0
        components = ((cached, rule.cached_input_price), (fresh, rule.input_price),
                      (output, rule.output_price), (reasoning, rule.reasoning_price))
        if any(amount > 0 and price is None for amount, price in components):
            return None
        return sum(amount * (price or 0) for amount, price in components) / 1_000_000


def _number(value: Any) -> float | None:
    return float(value) if value is not None else None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)
