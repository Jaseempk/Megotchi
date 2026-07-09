"""Nightly reflection ("sleep-time compute", layer 4) + the parent-readable
profile document (layer 5).

Reflection consolidates the day's episodes and the fact store into:
- a daily summary (episodic → narrative)
- an updated personality sketch (the "grown through you" identity)
- relationship notes (the child's social world)
then regenerates PROFILE.md — the single human-readable memory artifact that
doubles as the parental audit surface and the deletion/review mechanism.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .llm import LLM, parse_json_block
from .schemas import now_iso
from .store import Store

SYSTEM = """You are the nightly reflection process of a child's AI companion.
You consolidate raw conversation into durable understanding. Be faithful to the
evidence: never invent events, never diagnose, never speculate about mental
health. Write warmly but plainly, in English a parent can read.
The remembered-facts list is AUTHORITATIVE: your narrative must never
contradict it (if facts say a friendship ended, do not write that it endures).

Reply with ONLY a JSON object:
{
  "daily_summary": "2-4 sentences on what happened/was discussed today",
  "personality": "a running 3-6 sentence sketch of who this child is becoming,
                  updated from the previous sketch plus today's evidence",
  "relationships": "2-5 sentences on the people/pets in the child's world",
  "mood": "one short phrase, e.g. 'curious and playful' or 'a bit worried'"
}"""

REFLECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "daily_summary": {"type": "string"},
        "personality": {"type": "string"},
        "relationships": {"type": "string"},
        "mood": {"type": "string"},
    },
    "required": ["daily_summary", "personality", "relationships", "mood"],
}


def run_reflection(store: Store, llm: LLM, child: str, day_start: str,
                   day_end: str, ts: Optional[str] = None) -> dict:
    episodes = store.episodes_between(child, day_start, day_end)
    if not episodes:
        return {}
    convo = "\n".join(f"  {e.role}: {e.text}" for e in episodes)
    prev = store.latest_reflection(child, "personality")
    facts = store.current_facts(child)
    fact_lines = "\n".join(f"  - {f.text}" for f in facts[:40]) or "  (none)"

    user = (
        f"Child: {child}\nDate: {(ts or now_iso())[:10]}\n\n"
        f"Previous personality sketch:\n{prev.text if prev else '(none yet — blank slate)'}\n\n"
        f"Current remembered facts:\n{fact_lines}\n\n"
        f"Today's conversation:\n{convo}"
    )
    raw = llm.complete(SYSTEM, user, want_json=True, max_tokens=1200,
                       schema=REFLECTION_SCHEMA)
    try:
        data = parse_json_block(raw)
    except ValueError:
        # A failed reflection must never crash the loop — try once more,
        # then skip tonight; tomorrow's reflection sees the same episodes.
        raw = llm.complete(SYSTEM, user, want_json=True, max_tokens=1200,
                           schema=REFLECTION_SCHEMA)
        try:
            data = parse_json_block(raw)
        except ValueError:
            print(f"  ! reflection skipped: unparseable model reply ({raw[:80]!r})")
            return {}

    when = ts or now_iso()
    for kind in ("daily_summary", "personality", "relationships", "mood"):
        if data.get(kind):
            key = "daily" if kind == "daily_summary" else kind
            store.add_reflection(child, key, str(data[kind]), ts=when)
    return data


def render_profile(store: Store, child: str) -> str:
    """The visible, parent-editable memory document (Claude-memory pattern)."""
    personality = store.latest_reflection(child, "personality")
    relationships = store.latest_reflection(child, "relationships")
    daily = store.latest_reflection(child, "daily")
    mood = store.latest_reflection(child, "mood")
    facts = store.current_facts(child)
    past = store.superseded_facts(child)

    lines = [
        f"# {child.title()} & Megotchi — what the companion remembers",
        "",
        "*This document is regenerated after each reflection. It is the full,*",
        "*plain-language view of the companion's memory. Raw conversations stay*",
        "*on this device and are not shown here by design.*",
        "",
    ]
    if personality:
        lines += ["## Who they're becoming", personality.text, ""]
    if relationships:
        lines += ["## Their world", relationships.text, ""]
    if mood or daily:
        lines += ["## Lately"]
        if mood:
            lines.append(f"Mood: {mood.text}")
        if daily:
            lines.append(daily.text)
        lines.append("")

    if facts:
        lines.append("## Things Megotchi remembers")
        for f in facts:
            lines.append(f"- {f.text}  `({f.category}, importance {f.importance})`")
        lines.append("")
    if past:
        lines.append("## Things that changed (kept as history)")
        for f in past:
            since = (f.invalid_from or "")[:10]
            lines.append(f"- {f.text} — no longer current since {since}")
        lines.append("")
    return "\n".join(lines)


def write_profile(store: Store, child: str, data_dir: str | Path) -> Path:
    path = Path(data_dir) / "memory" / child / "PROFILE.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_profile(store, child), encoding="utf-8")
    return path
