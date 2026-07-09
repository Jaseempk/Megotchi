"""SQLite-backed memory store.

Layer 2 (episodic, append-only) and layer 3 (bi-temporal semantic facts) of the
five-layer architecture. Zero dependencies — stdlib sqlite3 only. Postgres
(Hindsight/Mem0-style) can replace this later behind the same interface.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .schemas import Episode, Fact, Operation, Reflection, now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    child TEXT NOT NULL,
    session TEXT NOT NULL,
    ts TEXT NOT NULL,
    role TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episodes_child_ts ON episodes(child, ts);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY,
    child TEXT NOT NULL,
    text TEXT NOT NULL,
    category TEXT NOT NULL,
    importance INTEGER NOT NULL,
    valid_from TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    invalid_from TEXT,
    superseded_by INTEGER,
    tombstoned INTEGER NOT NULL DEFAULT 0,
    provenance INTEGER
);
CREATE INDEX IF NOT EXISTS idx_facts_child ON facts(child, invalid_from, tombstoned);

CREATE TABLE IF NOT EXISTS reflections (
    id INTEGER PRIMARY KEY,
    child TEXT NOT NULL,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reflections_child ON reflections(child, kind, ts);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # ---------- episodes (append-only) ----------

    def add_episode(self, child: str, session: str, role: str, text: str,
                    ts: Optional[str] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO episodes (child, session, ts, role, text) VALUES (?,?,?,?,?)",
            (child, session, ts or now_iso(), role, text),
        )
        self.db.commit()
        return cur.lastrowid

    def recent_episodes(self, child: str, limit: int = 40) -> list[Episode]:
        rows = self.db.execute(
            "SELECT * FROM episodes WHERE child=? ORDER BY ts DESC, id DESC LIMIT ?",
            (child, limit),
        ).fetchall()
        return [Episode(**dict(r)) for r in reversed(rows)]

    def episodes_between(self, child: str, start_ts: str, end_ts: str) -> list[Episode]:
        rows = self.db.execute(
            "SELECT * FROM episodes WHERE child=? AND ts>=? AND ts<=? ORDER BY ts, id",
            (child, start_ts, end_ts),
        ).fetchall()
        return [Episode(**dict(r)) for r in rows]

    # ---------- facts (bi-temporal) ----------

    def _fact(self, row: sqlite3.Row) -> Fact:
        d = dict(row)
        d["tombstoned"] = bool(d["tombstoned"])
        return Fact(**d)

    def add_fact(self, child: str, text: str, category: str, importance: int,
                 valid_from: Optional[str] = None, provenance: Optional[int] = None,
                 recorded_at: Optional[str] = None) -> int:
        rec = recorded_at or now_iso()
        cur = self.db.execute(
            "INSERT INTO facts (child, text, category, importance, valid_from,"
            " recorded_at, provenance) VALUES (?,?,?,?,?,?,?)",
            (child, text, category, importance, valid_from or rec, rec, provenance),
        )
        self.db.commit()
        return cur.lastrowid

    def current_facts(self, child: str) -> list[Fact]:
        rows = self.db.execute(
            "SELECT * FROM facts WHERE child=? AND invalid_from IS NULL AND tombstoned=0"
            " ORDER BY importance DESC, recorded_at DESC",
            (child,),
        ).fetchall()
        return [self._fact(r) for r in rows]

    def superseded_facts(self, child: str, min_importance: int = 5) -> list[Fact]:
        """Past-but-important facts — the 'you used to…' capability."""
        rows = self.db.execute(
            "SELECT * FROM facts WHERE child=? AND invalid_from IS NOT NULL"
            " AND tombstoned=0 AND importance>=? ORDER BY importance DESC",
            (child, min_importance),
        ).fetchall()
        return [self._fact(r) for r in rows]

    def get_fact(self, fact_id: int) -> Optional[Fact]:
        row = self.db.execute("SELECT * FROM facts WHERE id=?", (fact_id,)).fetchone()
        return self._fact(row) if row else None

    @staticmethod
    def _normalize(text: str) -> str:
        """Normalize fact text for duplicate detection (FINDINGS F14)."""
        t = re.sub(r"\(as of [^)]*\)", "", text.lower())
        t = re.sub(r"[^a-z0-9 ]+", " ", t)
        return re.sub(r"\s+", " ", t).strip()

    def apply_operations(self, child: str, ops: Iterable[Operation],
                         provenance: Optional[int] = None,
                         ts: Optional[str] = None) -> list[str]:
        """Apply extractor operations. UPDATE supersedes (never edits in place);
        DELETE tombstones (never removes). Returns a human-readable audit log.

        Dedup guard (F14): small models double-write facts (ADD + UPDATE with
        the same text, or multi-supersede fan-out) and re-assert unchanged
        facts with fresh dates. Deterministic text-normalization catches both
        before they reach the store — no model in the loop."""
        log: list[str] = []
        when = ts or now_iso()
        seen: dict[str, int] = {self._normalize(f.text): f.id
                                for f in self.current_facts(child)}
        for op in ops:
            op = op.normalized()
            if op.op == "NOOP":
                continue
            norm = self._normalize(op.text) if op.text else ""
            if op.op == "ADD" and op.text:
                if norm in seen:
                    log.append(f"SKIP duplicate of #{seen[norm]}: {op.text}")
                    continue
                fid = self.add_fact(child, op.text, op.category, op.importance,
                                    valid_from=op.valid_from or when,
                                    provenance=provenance, recorded_at=when)
                seen[norm] = fid
                log.append(f"ADD #{fid}: {op.text}")
            elif op.op == "UPDATE" and op.text and not op.target_id:
                # Degrade a target-less UPDATE to ADD — losing the link is
                # better than losing the memory.
                if norm in seen:
                    log.append(f"SKIP duplicate of #{seen[norm]}: {op.text}")
                    continue
                fid = self.add_fact(child, op.text, op.category, op.importance,
                                    valid_from=op.valid_from or when,
                                    provenance=provenance, recorded_at=when)
                seen[norm] = fid
                log.append(f"ADD #{fid} (update w/o target): {op.text}")
            elif op.op == "UPDATE" and op.target_id and op.text:
                old = self.get_fact(op.target_id)
                if old is None or old.child != child:
                    continue
                if norm == self._normalize(old.text):
                    # Content unchanged — a date-refresh, not an update.
                    log.append(f"SKIP no-change update of #{old.id}")
                    continue
                if norm in seen and seen[norm] != old.id:
                    # The UPDATE half of an ADD+UPDATE double-write: the same
                    # new text already exists — don't supersede an unrelated
                    # fact with a duplicate.
                    log.append(f"SKIP duplicate-supersede of #{old.id}"
                               f" (text already at #{seen[norm]})")
                    continue
                # Guard against wrong-target UPDATEs (small models chain
                # target_id to the newest fact, not the matching one): a
                # cross-category supersede rewrites unrelated history — degrade
                # to ADD instead. Losing the link beats corrupting the past.
                if (old.category != op.category
                        and "other" not in (old.category, op.category)):
                    fid = self.add_fact(child, op.text, op.category, op.importance,
                                        valid_from=op.valid_from or when,
                                        provenance=provenance, recorded_at=when)
                    seen[norm] = fid
                    log.append(f"ADD #{fid} (blocked cross-category update"
                               f" of #{old.id}): {op.text}")
                    continue
                new_id = self.add_fact(child, op.text, op.category or old.category,
                                       max(op.importance, old.importance),
                                       valid_from=op.valid_from or when,
                                       provenance=provenance, recorded_at=when)
                self.db.execute(
                    "UPDATE facts SET invalid_from=?, superseded_by=? WHERE id=?",
                    (when, new_id, old.id),
                )
                self.db.commit()
                seen.pop(self._normalize(old.text), None)
                seen[norm] = new_id
                log.append(f"UPDATE #{old.id} -> #{new_id}: {op.text}")
            elif op.op == "DELETE" and op.target_id:
                old = self.get_fact(op.target_id)
                if old is None or old.child != child:
                    continue
                self.db.execute("UPDATE facts SET tombstoned=1 WHERE id=?", (old.id,))
                self.db.commit()
                log.append(f"TOMBSTONE #{old.id} ({op.reason or 'no reason given'})")
        return log

    # ---------- reflections ----------

    def add_reflection(self, child: str, kind: str, text: str,
                       ts: Optional[str] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO reflections (child, ts, kind, text) VALUES (?,?,?,?)",
            (child, ts or now_iso(), kind, text),
        )
        self.db.commit()
        return cur.lastrowid

    def latest_reflection(self, child: str, kind: str) -> Optional[Reflection]:
        row = self.db.execute(
            "SELECT * FROM reflections WHERE child=? AND kind=? ORDER BY ts DESC, id DESC LIMIT 1",
            (child, kind),
        ).fetchone()
        return Reflection(**dict(row)) if row else None

    def close(self) -> None:
        self.db.close()
