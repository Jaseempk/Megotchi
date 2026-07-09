"""Core data types for the Megotchi memory engine.

Design constraints (see ../MEGOTCHI-STRATEGY.md §1.6, §2):
- Episodes are append-only and never destroyed.
- Facts are bi-temporal: `valid_from` (event time) vs `recorded_at` (ingestion
  time). A superseded fact gets `invalid_from` + `superseded_by` — never erased.
- DELETE is a tombstone, never a row removal (COPPA deletion is handled at the
  store level by the parent, not by the model).
- Every fact carries provenance (the episode it came from) so the companion can
  never "remember" something no one said.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

FACT_CATEGORIES = (
    "person",       # friends, family, teachers
    "pet",
    "place",
    "preference",   # loves/hates
    "fear",
    "event",        # things that happened
    "milestone",    # first story written, learned to ride a bike
    "trait",        # observed personality traits
    "other",
)

# Facts at or above this importance never decay and may never be auto-removed
# by reflection ("the day the hamster died" outranks "we talked about lunch").
IMPORTANCE_FLOOR = 8


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Episode:
    id: int
    child: str
    session: str
    ts: str
    role: str  # "child" | "companion"
    text: str


@dataclass
class Fact:
    id: int
    child: str
    text: str
    category: str
    importance: int
    valid_from: str
    recorded_at: str
    invalid_from: Optional[str] = None
    superseded_by: Optional[int] = None
    tombstoned: bool = False
    provenance: Optional[int] = None  # episode id

    @property
    def current(self) -> bool:
        return self.invalid_from is None and not self.tombstoned


@dataclass
class Reflection:
    id: int
    child: str
    ts: str
    kind: str  # "daily" | "personality" | "relationships"
    text: str


@dataclass
class Operation:
    """One memory-update operation proposed by the extractor (Mem0 pattern)."""

    op: str  # ADD | UPDATE | DELETE | NOOP
    text: str = ""
    category: str = "other"
    importance: int = 3
    valid_from: Optional[str] = None
    target_id: Optional[int] = None  # for UPDATE / DELETE
    reason: str = ""

    def normalized(self) -> "Operation":
        self.op = self.op.upper().strip()
        if self.category not in FACT_CATEGORIES:
            self.category = "other"
        self.importance = max(1, min(10, int(self.importance or 3)))
        return self
