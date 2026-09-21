from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Any

@dataclass(frozen=True)
class Condition:
    key: str
    value: str

@dataclass
class QuerySeed:
    name: str
    topics: list[str]
    conditions: list[Condition]

@dataclass
class QuerySlice:
    seed_name: str
    topics: list[str]
    conditions: list[Condition]
    start_date: date
    end_date: date
    overflow_index: int = 0

@dataclass
class SearchPage:
    result_count: int
    results: list[dict[str, Any]]
    raw: dict[str, Any] = field(default_factory=dict)
