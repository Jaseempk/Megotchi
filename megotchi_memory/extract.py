"""Extraction + reconciliation (the Mem0 pattern, one pass).

After each exchange, the model sees the new exchange plus the child's current
facts and proposes ADD / UPDATE / DELETE / NOOP operations. The store enforces
the safety semantics regardless of what the model asks for: UPDATE supersedes,
DELETE tombstones — history is never destroyed.
"""
from __future__ import annotations

from typing import Optional

from .llm import LLM, parse_json_block
from .schemas import FACT_CATEGORIES, Operation
from .store import Store

SYSTEM = f"""You maintain the long-term memory of a child's AI companion.
Given a new conversation exchange and the list of currently remembered facts,
decide which memory operations to perform.

Rules:
- Only record things actually said or clearly implied. NEVER invent details.
- Prefer few, well-phrased, durable facts over many trivial ones. Skip small talk.
- Phrase facts in third person about the child, dated where relevant,
  e.g. "Best friend is Lena (as of 2026-07)".
- If a new statement contradicts or updates an existing fact, use UPDATE and
  you MUST set target_id to that fact's [id] from the list, plus the full
  corrected fact in "text" (the old fact is kept as history automatically).
- Write facts with absolute dates, never "yesterday"/"today".
- NEVER re-assert an existing fact with a new date. Only UPDATE a fact when
  its content actually changed.
- Pay special attention to relationship changes: if a friendship ends or a new
  close friend appears, UPDATE the old "best friend" fact to describe the new
  situation (e.g. "Fell out with Lena in May 2026; Aino is her close friend
  now"). Missing these is the worst failure you can make.
- Use DELETE ONLY if the child says a remembered fact was wrong or made up.
  Things ENDING is not deletion: a pet dying, a friendship ending, a fear
  being overcome are UPDATEs — the memory of how things were must be kept.
- "text" MUST always contain the complete new fact wording (for UPDATE too —
  write the corrected fact, not a description of the change). "reason" is
  short optional commentary only.
- importance 1-10: 8+ is reserved for life events and core relationships
  (a pet dying, a new sibling, a best friend). Everyday preferences are 3-5.
- Categories: {", ".join(FACT_CATEGORIES)}.

Reply with ONLY a JSON object:
{{"operations": [
  {{"op": "ADD", "text": "...", "category": "...", "importance": 5}},
  {{"op": "UPDATE", "target_id": 12, "text": "...", "category": "...", "importance": 6}},
  {{"op": "DELETE", "target_id": 7, "reason": "..."}}
]}}
Return {{"operations": []}} if nothing is worth remembering."""

# Grammar-enforced on Ollama (small local models need this; see llm.complete).
OPERATIONS_SCHEMA = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["ADD", "UPDATE", "DELETE", "NOOP"]},
                    "text": {"type": "string"},
                    "category": {"type": "string", "enum": list(FACT_CATEGORIES)},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                    "target_id": {"type": "integer"},
                    "reason": {"type": "string"},
                },
                "required": ["op", "text"],
            },
        }
    },
    "required": ["operations"],
}


def update_memory(store: Store, llm: LLM, child: str, child_said: str,
                  companion_said: str, provenance: Optional[int] = None,
                  ts: Optional[str] = None) -> list[str]:
    """Run one extract/reconcile pass for a single exchange. Returns audit log."""
    facts = store.current_facts(child)
    fact_lines = "\n".join(f"  [{f.id}] ({f.category}, imp {f.importance}) {f.text}"
                           for f in facts) or "  (none yet)"
    user = (
        f"Date of this conversation: {ts or 'today'}\n\n"
        f"Currently remembered facts about {child}:\n{fact_lines}\n\n"
        f"New exchange:\n"
        f"  {child}: {child_said}\n"
        f"  companion: {companion_said}\n"
    )
    raw = llm.complete(SYSTEM, user, want_json=True, schema=OPERATIONS_SCHEMA)
    try:
        data = parse_json_block(raw)
    except ValueError:
        return [f"extract: unparseable model reply ({raw[:120]!r})"]

    ops = []
    for item in data.get("operations", []):
        if not isinstance(item, dict) or "op" not in item:
            continue
        # Small local models sometimes put the new fact wording in "reason"
        # instead of "text" — salvage it rather than silently losing a memory.
        text = str(item.get("text", "")).strip()
        if not text and str(item.get("op", "")).upper() in ("ADD", "UPDATE"):
            text = str(item.get("reason", "")).strip()
        ops.append(Operation(
            op=str(item.get("op", "NOOP")),
            text=text,
            category=str(item.get("category", "other")),
            importance=int(item.get("importance", 3) or 3),
            valid_from=item.get("valid_from"),
            target_id=item.get("target_id"),
            reason=str(item.get("reason", "")),
        ))
    return store.apply_operations(child, ops, provenance=provenance, ts=ts)
