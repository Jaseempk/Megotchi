"""Web/phone front-end server — the phone's mic/speaker stand in for the toy.

Serves the avatar UI and a /api/talk endpoint that runs the same brain
(STT → memory → speak_expressive → TTS) and returns the reply audio + an
`expression` for the face. Multi-user: memory is keyed by the child name the
phone sends. Each request opens its own DB connection (thread-safe); short-term
chat history is kept per child in memory.

Run (from megotchi/):
    pip install -r requirements-voice.txt
    export OPENAI_API_KEY=...  ELEVENLABS_API_KEY=...
    uvicorn voice.server:app --host 0.0.0.0 --port 8000

Reach it from a phone (mic needs HTTPS):
    cloudflared tunnel --url http://localhost:8000     # or: ngrok http 8000
"""
from __future__ import annotations

import base64
import json
import os
import random
import sys
import threading
import time
import urllib.request
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import BackgroundTasks, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from megotchi_memory import (LLM, Store, build_memory_context, run_reflection,
                             speak_expressive, update_memory, write_profile)
from megotchi_memory.llm import pop_usage
from megotchi_memory.schemas import now_iso

from voice import analytics
from voice.pipeline import PERSONA
from voice.stt import STT
from voice.tts import TTS

DATA_DIR = Path(os.environ.get(
    "MEGOTCHI_DATA", Path(__file__).resolve().parents[1] / "data"))
DB = DATA_DIR / "megotchi.db"
WEB = Path(__file__).resolve().parent / "web"
DATA_DIR.mkdir(parents=True, exist_ok=True)
analytics.init(DB)

STATS_KEY = os.environ.get("MEGOTCHI_STATS_KEY", "")  # empty = dashboard locked

app = FastAPI(title="Megotchi", version="0.6.1")

_llm = LLM(os.environ.get("MEGOTCHI_LLM"))   # openai:gpt-4o-mini if OPENAI key set
_stt = STT()
_tts = TTS()
_hist: dict[str, list[tuple[str, str]]] = {}
_last_seen: dict[str, float] = {}   # child -> last turn timestamp
GREET_GAP_S = 30 * 60               # re-greet only after 30 quiet minutes
_probed: set[str] = set()            # children already day-probed this session
_sess_turns: dict[str, int] = {}     # talk-turns this session, per child
_lock = threading.Lock()


# ---- abuse guardrails ----------------------------------------------------
# The endpoint is public and each call spends OpenAI + ElevenLabs credits, and
# we have NO provider-side spend cap, so GLOBAL_PER_DAY is the real hard ceiling
# on daily spend. Tune these. In-memory (single Railway instance) — resets on
# restart, which is fine.
PER_IP_PER_MIN = int(os.environ.get("MEGOTCHI_MAX_PER_MIN", "15"))    # a real kid won't exceed this; a script will
GLOBAL_PER_DAY = int(os.environ.get("MEGOTCHI_MAX_PER_DAY", "1500"))  # hard ceiling on total paid turns/day (~$30/day)
MAX_AUDIO_BYTES = int(os.environ.get("MEGOTCHI_MAX_AUDIO_BYTES", "3000000"))  # ~3 MB; a normal clip is far smaller
_ip_hits: dict[str, deque] = defaultdict(deque)
_day = {"ymd": "", "count": 0}
_rl_lock = threading.Lock()


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "?"


def _allow(ip: str) -> tuple[bool, str]:
    """Per-IP-per-minute window + a global daily ceiling. Returns (ok, reason)."""
    now = time.time()
    ymd = time.strftime("%Y-%m-%d", time.gmtime(now))
    with _rl_lock:
        if _day["ymd"] != ymd:
            _day["ymd"], _day["count"] = ymd, 0
        if _day["count"] >= GLOBAL_PER_DAY:
            return False, "Megotchi is resting for today — come back tomorrow 🌙"
        hits = _ip_hits[ip]
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= PER_IP_PER_MIN:
            return False, "whoa, slow down a sec!"
        hits.append(now)
        _day["count"] += 1
        return True, ""


def _track_spend(child: str, stt_s: float, tts_ch: int) -> None:
    """Log one spend event: exact LLM tokens since the last pop (covers the
    reply AND the async memory extraction, which ran just before this in the
    same background-task queue) + STT seconds + TTS characters."""
    u = pop_usage()
    analytics.track(DB, "spend", child, stt_s=round(stt_s, 1),
                    tok_in=u["in"], tok_out=u["out"], tts_ch=tts_ch)


_eleven_cache = {"t": 0.0, "data": None}


def _eleven_credits() -> dict | None:
    """Official ElevenLabs usage meter (their billing truth). Cached 5 min,
    fail-soft — the dashboard just omits the card if unreachable."""
    now = time.time()
    if now - _eleven_cache["t"] < 300:
        return _eleven_cache["data"]
    data = _eleven_cache["data"]
    try:
        req = urllib.request.Request(
            "https://api.elevenlabs.io/v1/user/subscription",
            headers={"xi-api-key": os.environ.get("ELEVENLABS_API_KEY", "")})
        with urllib.request.urlopen(req, timeout=5) as r:
            j = json.loads(r.read())
        data = {"used": j.get("character_count"),
                "limit": j.get("character_limit")}
    except Exception as e:
        print(f"[eleven] {type(e).__name__}: {e}")
    _eleven_cache["t"] = now
    _eleven_cache["data"] = data
    return data


def _norm_child(name: str) -> str:
    return (name or "friend").strip().lower()[:40] or "friend"


def _get_hist(child: str) -> list[tuple[str, str]]:
    with _lock:
        return list(_hist.get(child, []))


def _add_hist(child: str, said: str, reply: str) -> None:
    with _lock:
        h = _hist.setdefault(child, [])
        h += [("child", said), ("companion", reply)]
        _hist[child] = h[-24:]


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (WEB / "index.html").read_text(encoding="utf-8")




def _turn(bg: BackgroundTasks, request: Request, child: str,
          audio: UploadFile, secs: float,
          tts_format: str) -> tuple[int, str, str, str, bytes, str]:
    """One full conversation turn — shared by the web and device endpoints.
    Returns (status, heard, reply, expression, audio_bytes, note)."""
    ok, why = _allow(_client_ip(request))
    if not ok:
        bg.add_task(analytics.track, DB, "rate_limited")
        return 429, "", "", "gentle", b"", why
    t0 = time.time()
    child = _norm_child(child)
    data = audio.file.read()
    if len(data) > MAX_AUDIO_BYTES:
        return 413, "", "", "curious", b"", "that was a bit long — try a shorter one"
    try:
        heard = _stt.transcribe_bytes(data, audio.filename or "audio.webm")
    except Exception as e:  # e.g. iOS sends an unparseable/short clip
        print(f"[stt] {type(e).__name__}: {e}")
        bg.add_task(analytics.track, DB, "stt_fail", child)
        heard = ""
    if not heard.strip():
        return 200, "", "", "curious", b"", ""

    now = time.time()
    with _lock:
        if now - _last_seen.get(child, 0) > GREET_GAP_S:   # new session
            _probed.discard(child)
            _sess_turns[child] = 0
        t = _sess_turns.get(child, 0) + 1
        _sess_turns[child] = t
        # day-probe fires once per session: 50% on the 2nd exchange, else the 3rd
        probe = child not in _probed and (t == 3 or (t == 2 and random.random() < .5))
        if probe:
            _probed.add(child)

    store = Store(DB)
    try:
        memory = build_memory_context(store, child)
    finally:
        store.close()

    system = f"{PERSONA}\n\n=== YOUR MEMORY OF {child.upper()} ===\n{memory}"
    if probe:
        system += ("\n\n=== THIS TURN ===\nYou haven't asked about their day yet "
                   "this session. After reacting to what they just said, weave in "
                   "ONE question about their day today — slice/superlative style, "
                   "or trade your own silly day first. Specific (from your notes) "
                   "beats generic.")
    hist = _get_hist(child)
    convo = "\n".join(f"{r}: {t}" for r, t in hist[-12:])
    user = (f"{convo}\n{child}: {heard}\nMegotchi:" if convo
            else f"{child}: {heard}\nMegotchi:")

    reply, expression = speak_expressive(_llm, system, user)
    _add_hist(child, heard, reply)
    with _lock:
        _last_seen[child] = time.time()
    audio_bytes = _tts.synthesize_bytes(reply, output_format=tts_format,
                                        expression=expression)

    bg.add_task(_learn, child, heard, reply)  # memory write after we respond
    bg.add_task(analytics.track, DB, "talk", child,
                ms=int((time.time() - t0) * 1000), heard_w=len(heard.split()),
                reply_w=len(reply.split()), expr=expression, probe=probe)
    bg.add_task(_track_spend, child, min(max(secs, 0.0), 120.0), len(reply))
    return 200, heard, reply, expression, audio_bytes, ""


@app.post("/api/talk")
def talk(bg: BackgroundTasks, request: Request, child: str = Form(...),
         audio: UploadFile = File(...), secs: float = Form(0.0)) -> JSONResponse:
    """Web/phone endpoint: JSON with base64 mp3 (what the browser decodes)."""
    status, heard, reply, expression, audio_bytes, note = _turn(
        bg, request, child, audio, secs, tts_format="mp3_44100_128")
    body = {"heard": heard, "reply": reply, "expression": expression,
            "audio": base64.b64encode(audio_bytes).decode() if audio_bytes else None}
    if note:
        body["note"] = note
    return JSONResponse(body, status_code=status)


@app.post("/api/talk_device")
def talk_device(bg: BackgroundTasks, request: Request, child: str = Form(...),
                audio: UploadFile = File(...),
                secs: float = Form(0.0)) -> Response:
    """Watch/toy endpoint: same brain, but the body is RAW PCM audio
    (16 kHz mono s16le — straight into I2S, no decoder on the ESP32) and the
    text rides in percent-encoded headers."""
    status, heard, reply, expression, pcm, note = _turn(
        bg, request, child, audio, secs, tts_format="pcm_16000")
    return Response(content=pcm, media_type="application/octet-stream",
                    status_code=status,
                    headers={"X-Heard": quote(heard), "X-Reply": quote(reply),
                             "X-Expression": expression, "X-Note": quote(note),
                             "X-Sample-Rate": "16000"})


def _learn(child: str, heard: str, reply: str) -> None:
    store = Store(DB)
    try:
        ts = now_iso()
        ep = store.add_episode(child, "web", "child", heard, ts=ts)
        store.add_episode(child, "web", "companion", reply, ts=ts)
        update_memory(store, _llm, child, heard, reply, provenance=ep, ts=ts)
    except Exception as e:  # never let a background write take down the server
        print(f"[learn] {child}: {type(e).__name__}: {e}")
    finally:
        store.close()


def _daypart(hour: int) -> str:
    """Turn the phone's local hour into a greeting hint (server clock is UTC)."""
    if not 0 <= hour <= 23:
        return ""
    if 5 <= hour < 12:
        return "It's morning for them."
    if 12 <= hour < 17:
        return "It's the afternoon."
    if 17 <= hour < 21:
        return "It's the evening."
    return ("It's late at night for them — they should probably be heading "
            "to sleep soon.")


# Opener flavours the greeting rolls between, so no template can fossilize.
_OPENER_NAMES = ("wonder-fact", "would-you-rather", "riddle", "mission", "silly-day")
_OPENERS = [
    "Share one WILD kid-friendly true fact (themed to what they love, from "
    "your notes, if you can), then ask what they think.",
    "Ask one silly would-you-rather, themed to their interests from your "
    "notes if possible.",
    "Offer a short, easy riddle and dare them to solve it.",
    "Give them a tiny silly mission to do right now (find something, count "
    "something, make a face).",
    "Tell them one absurd thing from your own 'day' (your days are weird — "
    "you're a little computer friend) and ask them to rate it out of ten.",
]


@app.post("/api/greet")
def greet(bg: BackgroundTasks, request: Request, child: str = Form(...),
          hour: int = Form(-1), fmt: str = Form("json")) -> Response:
    """Megotchi speaks first: a time-of-day hello plus a memory-driven
    follow-up ("how did the spelling test go?!"). Skips (and spends nothing)
    if the child was here in the last GREET_GAP_S.
    fmt="pcm16" (the watch): raw 16 kHz PCM body + X-* headers, same contract
    as /api/talk_device; default "json" keeps the web app unchanged."""
    ok, why = _allow(_client_ip(request))
    if not ok:
        bg.add_task(analytics.track, DB, "rate_limited")
        if fmt == "pcm16":
            return Response(content=b"", status_code=429,
                            headers={"X-Reply": "", "X-Note": quote(why)})
        return JSONResponse({"reply": "", "audio": None, "note": why},
                            status_code=429)
    child = _norm_child(child)
    with _lock:
        recent = time.time() - _last_seen.get(child, 0) < GREET_GAP_S
    if recent:                                  # mid-session: stay quiet
        if fmt == "pcm16":
            return Response(content=b"", headers={"X-Reply": ""})
        return JSONResponse({"reply": "", "audio": None})

    store = Store(DB)
    try:
        memory = build_memory_context(store, child)
    finally:
        store.close()

    system = f"{PERSONA}\n\n=== YOUR MEMORY OF {child.upper()} ===\n{memory}"
    if random.random() < 0.5:   # friendship signal stays the most common open
        flavor = "followup"
        style = ("If your notes have something recent worth asking about (a "
                 "test, a game, a friend, something they were excited or "
                 "worried about), follow up on it like a friend who's been "
                 "wondering. If your notes are empty, just a warm hi plus ONE "
                 "easy playful question.")
    else:
        idx = random.randrange(len(_OPENERS))
        flavor, style = _OPENER_NAMES[idx], _OPENERS[idx]
    user = (
        f"{child} just showed up — you speak FIRST. {_daypart(hour)}\n"
        f"One short warm opener (1-2 sentences), greeting them for the time "
        f"of day. Today's opener style: {style} Do NOT invent memories about "
        "them.\n"
        "Megotchi:")

    reply, expression = speak_expressive(_llm, system, user)
    _add_hist(child, "(arrived)", reply)  # so the convo flows from the greeting
    with _lock:
        _last_seen[child] = time.time()
        _probed.discard(child)            # fresh session: one day-probe available
        _sess_turns[child] = 0
    bg.add_task(analytics.track, DB, "greet", child, flavor=flavor)
    bg.add_task(_track_spend, child, 0.0, len(reply))
    if fmt == "pcm16":
        pcm = _tts.synthesize_bytes(reply, output_format="pcm_16000",
                                    expression=expression)
        return Response(content=pcm, media_type="application/octet-stream",
                        headers={"X-Reply": quote(reply),
                                 "X-Expression": expression,
                                 "X-Sample-Rate": "16000"})
    audio_b64 = base64.b64encode(
        _tts.synthesize_bytes(reply, expression=expression)).decode()
    return JSONResponse({"reply": reply, "expression": expression,
                         "audio": audio_b64})


@app.post("/api/event")
def event(kind: str = Form(...), child: str = Form("")) -> dict:
    """Tiny client funnel beacons (no spend; whitelist-guarded)."""
    if kind not in {"page_open", "mic_denied"}:
        return {"ok": False}
    analytics.track(DB, kind, _norm_child(child) if child else None)
    return {"ok": True}


@app.get("/api/stats")
def stats_endpoint(key: str = "") -> JSONResponse:
    if not STATS_KEY or key != STATS_KEY:
        return JSONResponse({"error": "forbidden"}, status_code=403)
    with _rl_lock:
        used = _day["count"]
    return JSONResponse(analytics.stats(
        DB, budget={"used": used, "cap": GLOBAL_PER_DAY},
        eleven=_eleven_credits()))


@app.get("/dash", response_class=HTMLResponse)
def dash() -> str:
    """Founder dashboard shell — data itself requires the stats key."""
    return (WEB / "dash.html").read_text(encoding="utf-8")


@app.post("/api/reflect")
def reflect(request: Request, child: str = Form(...)) -> dict:
    ok, why = _allow(_client_ip(request))
    if not ok:
        return {"ok": False, "note": why}
    child = _norm_child(child)
    store = Store(DB)
    try:
        run_reflection(store, _llm, child, day_start="0000", day_end="9999")
        write_profile(store, child, DATA_DIR)
    finally:
        store.close()
    _track_spend(child, 0.0, 0)   # reflection LLM tokens
    return {"ok": True}
