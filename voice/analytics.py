"""First-party analytics for the voice server — no third-party trackers.

Two sources, one SQLite file:
- the memory engine's `episodes` table (already written for every turn):
  sessions, retention and per-kid stats are DERIVED from it, so all history
  counts retroactively from the day this module ships;
- a small `events` table for what episodes can't see: greet flavours,
  latency, expressions, errors, rate-limit hits and client funnel beacons.

Stdlib only. track() never raises — analytics must never break the app.
Days are UTC (matching the server's rate-limit day).
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone

DAY = 86400
SESSION_GAP_S = 30 * 60   # same session boundary the server's greeting uses

# gpt-4o-mini list prices; STT is gpt-4o-mini-transcribe per audio minute.
P_IN = 0.15 / 1e6         # $/input token
P_OUT = 0.60 / 1e6        # $/output token
P_STT_MIN = 0.003         # $/minute of audio


def _conn(db_path) -> sqlite3.Connection:
    return sqlite3.connect(str(db_path), timeout=10)


def init(db_path) -> None:
    with _conn(db_path) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY,
            ts REAL NOT NULL,
            kind TEXT NOT NULL,
            child TEXT,
            data TEXT)""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        c.execute("PRAGMA journal_mode=WAL")  # dashboard reads never block turn writes


def track(db_path, kind: str, child: str | None = None, **data) -> None:
    """Insert one event. Never raises — analytics must not take down a turn."""
    try:
        with _conn(db_path) as c:
            c.execute("INSERT INTO events (ts, kind, child, data) VALUES (?,?,?,?)",
                      (time.time(), kind, child,
                       json.dumps(data) if data else None))
    except Exception as e:
        print(f"[analytics] {kind}: {type(e).__name__}: {e}")


def _epoch(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def _sessions(times: list[float]) -> list[dict]:
    """Group sorted turn timestamps into sessions split by SESSION_GAP_S."""
    out: list[dict] = []
    for t in times:
        if out and t - out[-1]["end"] <= SESSION_GAP_S:
            out[-1]["end"] = t
            out[-1]["turns"] += 1
        else:
            out.append({"start": t, "end": t, "turns": 1})
    return out


def _day_label(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%d %b")


def stats(db_path, budget: dict | None = None,
          eleven: dict | None = None) -> dict:
    """All dashboard aggregates in one pass. Cheap at dogfood scale."""
    now = time.time()
    today = int(now // DAY)
    wk = now - 7 * DAY

    con = _conn(db_path)
    try:
        eps = con.execute("SELECT child, ts, text FROM episodes "
                          "WHERE role='child' ORDER BY child, ts").fetchall()
        try:
            evs = con.execute("SELECT ts, kind, child, data FROM events "
                              "WHERE ts > ? ORDER BY ts",
                              (now - 35 * DAY,)).fetchall()
        except sqlite3.OperationalError:  # table not created yet
            evs = []
        try:  # spend is aggregated over ALL time, not the 35-day event window
            sp = con.execute("SELECT ts, data FROM events "
                             "WHERE kind='spend'").fetchall()
        except sqlite3.OperationalError:
            sp = []
    finally:
        con.close()

    # ---- per-kid turn history (epoch, word-count) from episodes ----
    turns: dict[str, list[tuple[float, int]]] = {}
    for child, ts, text in eps:
        try:
            turns.setdefault(child, []).append((_epoch(ts), len(text.split())))
        except ValueError:
            pass
    for v in turns.values():
        v.sort()
    sess = {c: _sessions([t for t, _ in v]) for c, v in turns.items()}
    all_turn_times = sorted(t for v in turns.values() for t, _ in v)

    # ---- "right now" strip ----
    strip = {
        "active_now": sorted(c for c, v in turns.items() if now - v[-1][0] < 300),
        "kids_today": sum(1 for v in turns.values()
                          if any(int(t // DAY) == today for t, _ in v)),
        "turns_today": sum(1 for t in all_turn_times if int(t // DAY) == today),
        "budget": budget or {},
    }

    # ---- 14-day activity ----
    days = []
    for i in range(13, -1, -1):
        a, b = (today - i) * DAY, (today - i + 1) * DAY
        days.append({
            "d": _day_label(a),
            "sessions": sum(1 for ss in sess.values()
                            for s in ss if a <= s["start"] < b),
            "turns": sum(1 for t in all_turn_times if a <= t < b),
            "kids": sum(1 for v in turns.values()
                        if any(a <= t < b for t, _ in v)),
        })

    # ---- retention cohorts ----
    def _ret(offsets, min_age_days):
        eligible = returned = 0
        for v in turns.values():
            d0 = int(v[0][0] // DAY)
            if today - d0 < min_age_days:
                continue
            eligible += 1
            active = {int(t // DAY) for t, _ in v}
            if any((d0 + o) in active for o in offsets):
                returned += 1
        return {"returned": returned, "eligible": eligible}

    retention = {"d1": _ret((1,), 1),
                 "d7": _ret((6, 7, 8), 6),
                 "d30": _ret(range(27, 34), 27)}

    # ---- session quality ----
    all_sessions = [dict(s, child=c) for c, ss in sess.items() for s in ss]

    def _fmt_sess(s):
        return {"child": s["child"], "turns": s["turns"],
                "minutes": round((s["end"] - s["start"]) / 60, 1),
                "date": _day_label(s["start"])}

    key = lambda s: (s["turns"], s["end"] - s["start"])
    today_sessions = [s for s in all_sessions if int(s["start"] // DAY) == today]
    n = len(all_sessions)
    quality = {
        "total_sessions": n,
        "avg_turns": round(sum(s["turns"] for s in all_sessions) / n, 1) if n else 0,
        "avg_minutes": round(sum(s["end"] - s["start"] for s in all_sessions) / n / 60, 1) if n else 0,
        "longest": _fmt_sess(max(all_sessions, key=key)) if all_sessions else None,
        "longest_today": _fmt_sess(max(today_sessions, key=key)) if today_sessions else None,
    }

    # ---- per-kid table ----
    kids_rows = []
    for c, v in turns.items():
        ss = sess[c]
        kids_rows.append({
            "child": c,
            "first_seen": _day_label(v[0][0]),
            "last_seen_min": int((now - v[-1][0]) / 60),
            "turns": len(v),
            "sessions": len(ss),
            "sessions_7d": sum(1 for s in ss if s["start"] > wk),
            "avg_turns": round(len(v) / len(ss), 1),
            "avg_words": round(sum(w for _, w in v) / len(v), 1),
        })
    kids_rows.sort(key=lambda r: r["last_seen_min"])

    # ---- events: flavours, probes, latency, expressions, funnel ----
    flavors: dict[str, list[int]] = {}
    probes_fired = probes_answered = 0
    probe_words: list[int] = []
    lat: list[int] = []
    expr: dict[str, int] = {}
    funnel = {"page_open_7d": 0, "mic_denied_7d": 0, "stt_fail_7d": 0,
              "rate_limited_today": 0, "errors_today": 0}

    for ts, kind, child, data in evs:
        d = json.loads(data) if data else {}
        kid_turns = turns.get(child, [])
        if kind == "greet":
            after = sum(1 for t, _ in kid_turns if ts < t <= ts + SESSION_GAP_S)
            flavors.setdefault(d.get("flavor", "?"), []).append(after)
        elif kind == "talk":
            if ts > wk:
                if "ms" in d:
                    lat.append(d["ms"])
                if d.get("expr"):
                    expr[d["expr"]] = expr.get(d["expr"], 0) + 1
            if d.get("probe"):
                probes_fired += 1
                nxt = next(((t, w) for t, w in kid_turns
                            if ts + 1 < t <= ts + 900), None)
                if nxt:
                    probes_answered += 1
                    probe_words.append(nxt[1])
        elif kind == "page_open" and ts > wk:
            funnel["page_open_7d"] += 1
        elif kind == "mic_denied" and ts > wk:
            funnel["mic_denied_7d"] += 1
        elif kind == "stt_fail":
            if ts > wk:
                funnel["stt_fail_7d"] += 1
            if int(ts // DAY) == today:
                funnel["errors_today"] += 1
        elif kind == "rate_limited" and int(ts // DAY) == today:
            funnel["rate_limited_today"] += 1
    funnel["talkers_7d"] = sum(1 for v in turns.values() if v[-1][0] > wk)

    # ---- spend: exact tokens/seconds/chars priced at list rates ----
    tts_usd_1k = float(os.environ.get("MEGOTCHI_TTS_USD_PER_1K_CHARS", "0.22"))
    spend = {k: {"oai_usd": 0.0, "tts_ch": 0, "n": 0}
             for k in ("today", "wk", "all")}
    for ts, data in sp:
        d = json.loads(data) if data else {}
        oai = (d.get("tok_in", 0) * P_IN + d.get("tok_out", 0) * P_OUT
               + d.get("stt_s", 0) / 60 * P_STT_MIN)
        ch = d.get("tts_ch", 0)
        buckets = ["all"]
        if ts > wk:
            buckets.append("wk")
        if int(ts // DAY) == today:
            buckets.append("today")
        for k in buckets:
            b = spend[k]
            b["oai_usd"] += oai
            b["tts_ch"] += ch
            b["n"] += 1
    for b in spend.values():
        b["tts_usd"] = round(b["tts_ch"] / 1000 * tts_usd_1k, 4)
        b["oai_usd"] = round(b["oai_usd"], 4)
        b["total_usd"] = round(b["oai_usd"] + b["tts_usd"], 4)
    per_interaction = (round(spend["all"]["total_usd"] / spend["all"]["n"], 4)
                       if spend["all"]["n"] else None)
    tts_days_left = None
    if (eleven and eleven.get("limit") and eleven.get("used") is not None
            and spend["wk"]["tts_ch"]):
        remaining = eleven["limit"] - eleven["used"]
        tts_days_left = max(0.0, round(remaining / (spend["wk"]["tts_ch"] / 7), 1))

    lat.sort()
    return {
        "spend": {**spend, "per_interaction": per_interaction,
                  "eleven": eleven, "tts_days_left": tts_days_left,
                  "tts_usd_per_1k": tts_usd_1k},
        "now": strip,
        "days": days,
        "retention": retention,
        "quality": quality,
        "kids": kids_rows,
        "flavors": sorted(
            [{"flavor": f, "greets": len(a),
              "avg_turns_after": round(sum(a) / len(a), 1)}
             for f, a in flavors.items()],
            key=lambda r: -r["avg_turns_after"]),
        "probes": {"fired": probes_fired, "answered": probes_answered,
                   "avg_words": round(sum(probe_words) / len(probe_words), 1)
                   if probe_words else 0},
        "funnel": funnel,
        "latency": {"avg_ms": int(sum(lat) / len(lat)) if lat else None,
                    "p90_ms": lat[min(len(lat) - 1, int(0.9 * len(lat)))]
                    if lat else None},
        "expressions": expr,
        "generated": now,
    }
