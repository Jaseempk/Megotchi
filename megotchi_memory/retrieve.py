"""Serve memory into the conversation (layer 1: curated working context).

Per ConvoMem, early-history retrieval can be simple: the profile sketch + all
current facts + important past facts fits comfortably in context for months of
use. Scoring (recency × importance × relevance, Generative-Agents style) kicks
in only when the store outgrows the budget.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .schemas import Fact
from .store import Store

MAX_FACTS = 60
MAX_PAST_FACTS = 15


def _score(f: Fact, now: datetime) -> float:
    """Recency x importance (relevance/embeddings arrive in v1)."""
    try:
        rec = datetime.fromisoformat(f.recorded_at)
        if rec.tzinfo is None:
            rec = rec.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - rec).total_seconds() / 86400)
    except ValueError:
        age_days = 365.0
    # Slow decay tuned for years, with an importance floor: core memories
    # (importance >= 8) never decay at all.
    recency = 1.0 if f.importance >= 8 else 0.995 ** age_days
    return f.importance * recency


def build_memory_context(store: Store, child: str) -> str:
    now = datetime.now(timezone.utc)
    personality = store.latest_reflection(child, "personality")
    relationships = store.latest_reflection(child, "relationships")
    facts = store.current_facts(child)
    past = store.superseded_facts(child)

    if len(facts) > MAX_FACTS:
        facts = sorted(facts, key=lambda f: _score(f, now), reverse=True)[:MAX_FACTS]

    parts: list[str] = []
    if personality:
        parts += ["Who this child is (from living with them):", personality.text, ""]
    if relationships:
        parts += ["Their world:", relationships.text, ""]
    if facts:
        parts.append("Facts you remember (only ever reference THESE — never invent memories):")
        parts += [f"- {f.text}" for f in facts]
        parts.append("")
    if past:
        parts.append("Things that used to be true (for 'remember when' moments):")
        parts += [f"- {f.text} (changed {(f.invalid_from or '')[:10]})" for f in past[:MAX_PAST_FACTS]]
        parts.append("")
    if not parts:
        parts = ["You have no memories of this child yet — you were just born. "
                 "Be curious and start getting to know them."]
    return "\n".join(parts)
