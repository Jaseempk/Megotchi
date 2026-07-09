# Megotchi — System Architecture

*The reference for how Megotchi is built. Companion docs: `../MEGOTCHI-STRATEGY.md`
(why), `LOCAL-MODEL-HARDWARE.md` (brain/hardware choices), `FINDINGS.md`
(memory-engine failure catalog), `evals/RESULTS.md` (benchmark log).*

---

## 1. The core idea: two loops, not one

Every shipped AI toy is a single loop — hear, answer, speak. Megotchi adds a
second, asynchronous loop that is the entire moat.

```
REALTIME LOOP  (must feel alive, ~1–2s)
  child speaks ─▶ STT ─▶ retrieve memory ─▶ LLM composes grounded reply ─▶ TTS ─▶ toy speaks
                              ▲
                              │ reads
BACKGROUND LOOP  (latency-tolerant)        MEMORY STORE  (the soul — plain data)
  after each exchange ─▶ extract + reconcile facts ─┘ writes
  nightly (on charger) ─▶ reflect: episodes → personality, regenerate PROFILE.md
  (Phase 2) ───────────▶ derive patterns → parent dashboard
```

Strip the second loop and you have FoloToy (which literally has no database —
see `../MEGOTCHI-STRATEGY.md`). The realtime loop is commodity; the background
loop + the memory it maintains is Megotchi.

**Load-bearing design decision:** *personality lives in the memory store (text
records + reflections), not in model weights.* Consequences:
- Portable across hardware generations — the "soul" is a small file.
- Auditable — parents can read it (`PROFILE.md`).
- Swappable brain — see §7.

---

## 2. Component map

```
┌─ TOY (ESP32-S3-AMOLED-2.06) ──────────┐     ┌─ CLOUD SERVER — the brain ─────────────┐
│  wake word / button                    │     │  Voice pipeline (cascade)              │
│  dual mic → stream audio  ─────────────┼────▶│    STT · retrieve · LLM(speak) · TTS   │
│  speaker ← stream reply audio ◀────────┼─────┤  Memory engine  (megotchi_memory)      │
│  AMOLED face · haptic motor            │     │    episodic + bi-temporal facts         │
└────────────────────────────────────────┘     │    extract · reflect · retrieve         │
   WebSocket:  LAN in dev,  WSS internet in prod│  PROFILE.md  (parent-readable memory)   │
                                                └────────────────────────────────────────┘
                                                             │ (Phase 2)
                                                             ▼
                                                   ┌─ PARENT DASHBOARD (web) ─┐
                                                   │  patterns, not transcripts │
                                                   └────────────────────────────┘
```

The toy is a thin client: wake word + audio streaming + face/animation + motor.
Everything intelligent runs on **the cloud server** (see the terminology note
below). The MVP has **no home hub** — the toy talks to a server we operate.

> **Terminology — "server" vs "hub" (they are NOT the same):**
> - **Cloud server (MVP, this doc):** a server *we* run that hosts the pipeline.
>   In development it is the dev's Mac on the LAN (the toy's mic/speaker are
>   stood in by the Mac's until firmware exists); in production it is a cloud
>   instance (Fly.io / Railway / VPS / AWS). It is **cheap and GPU-less** — STT,
>   LLM, and TTS are external API calls, so the server just *orchestrates* and
>   holds the memory store. SQLite in dev → Postgres in multi-family prod.
> - **Home hub (local-first roadmap, §7, NOT the MVP):** a device *in the
>   family's home* (parent's phone, or a bundled dock) that the brain migrates
>   *onto* later, to pull inference off the cloud for privacy. The MVP does not
>   have one; it appears only at v2/v3.

---

## 3. MVP architecture (cloud-first, concrete)

Decisions locked for v0:

| Stage | Choice | Adapter | Swap-before-launch? |
|---|---|---|---|
| STT | OpenAI `gpt-4o-mini-transcribe` | `voice/stt.py` | maybe (Deepgram for latency) |
| LLM (answer + memory ops) | OpenAI `gpt-4o-mini` | `megotchi_memory/llm.py` | → Bedrock (ToS for under-13) |
| TTS | ElevenLabs (`eleven_flash_v2_5`) | `voice/tts.py` | **yes** — ElevenLabs bans under-13 |
| Memory | `megotchi_memory` (SQLite) | built | on-device target later |
| Transport | WebSocket (dev: Mac on LAN · prod: cloud server) | `voice/run_mac.py`, `voice/server.py` | — |

Every provider sits behind an adapter with the same shape as `llm.py`
(`provider:model` spec, env-selectable), so none of these choices are load-bearing.

**Why cascade, not speech-to-speech:** the background loop needs the *text* of
each turn to extract facts, and the reply must be *grounded* in retrieved
memory. A fused speech-to-speech model (OpenAI Realtime) is lower-latency but
hides the text and the injection point. Cascade keeps both under our control.

---

## 4. The memory engine (`megotchi_memory/`) — already built & eval'd

Five layers (research basis in `../MEGOTCHI-STRATEGY.md §1.6`):

1. **Working context** (`retrieve.py`) — curated persona + facts injected into
   the prompt. Scored recency × importance; importance ≥ 8 never decays.
2. **Episodic store** (`store.py`) — append-only, timestamped raw turns. Never
   destroyed.
3. **Semantic facts** (`store.py`) — bi-temporal: `valid_from` vs `recorded_at`;
   contradicted facts get `invalid_from` + `superseded_by` (kept as history →
   "you used to be scared of the dark"). DELETE = tombstone, never a row removal.
4. **Reflection** (`reflect.py`) — nightly "sleep-time" consolidation:
   episodes → personality sketch, relationships, mood; regenerates `PROFILE.md`.
5. **Parent-readable artifact** (`PROFILE.md`) — the single human-readable view;
   doubles as the audit + deletion surface.

**Pipeline modules:**
- `extract.py` — after each exchange, LLM proposes ADD / UPDATE / DELETE / NOOP;
  the store enforces the semantics regardless of what the model asks.
- `speech.py` — `speak()` produces the spoken reply via grammar-constrained JSON
  (`{recall, reply}`) so deliberation can't leak into speech (`clean_speech()` is
  the fallback salvage). See FINDINGS F20.
- `llm.py` — provider-agnostic: `ollama:` / `anthropic:` / `openai:`.

**Design invariants (do not break — each exists because of an observed failure,
see FINDINGS):**
- Model output is **untrusted input**; the store enforces invariants, prompts
  only raise the hit rate.
- Episodes append-only; facts superseded not edited; DELETE tombstones.
- Importance ≥ 8 never decays; every fact carries provenance (no fabricated
  memories).
- Prefer *degradation* to *loss* (a mislinked memory is repairable; a dropped
  one is gone).

---

## 5. The voice pipeline (`voice/`) — Phase A build

```
VoicePipeline.handle_audio(wav) ─▶ (heard, reply, reply_audio)     # fast path
    heard      = STT.transcribe(wav)
    memory     = build_memory_context(store, child)
    reply      = speak(llm, PERSONA + memory, f"{child}: {heard}")
    reply_audio= TTS.synthesize(reply)

VoicePipeline.learn(heard, reply)                                  # async, after playback
    store.add_episode(...)  ×2
    update_memory(store, llm, child, heard, reply)                # extract + reconcile
```

The split matters: the reply is produced and spoken *before* memory extraction
runs, so the write (an extra LLM call) never adds latency to what the child
hears — the async-writes principle from the strategy.

**Adapters** (`stt.py`, `tts.py`) mirror `LLM`: `Provider(spec)` with a single
method (`transcribe` / `synthesize`), env-selectable (`MEGOTCHI_STT`,
`MEGOTCHI_TTS`), deferred SDK imports.

**Transports** into the same pipeline:
- `run_mac.py` — push-to-talk on the Mac (Phase A, testable today).
- `server.py` — WebSocket for the ESP32 (Phase C). Same `VoicePipeline`.

---

## 6. Data & where it lives

| Data | MVP location | Target location | Notes |
|---|---|---|---|
| Raw episodes (transcripts) | server SQLite | **on-device** | never leaves home at target |
| Semantic facts | server SQLite | on-device + encrypted sync | bi-temporal |
| `PROFILE.md` | server file | on-device + parent app | the audit/delete surface |
| Derived patterns | (Phase 2) | encrypted family cloud | dashboard reads these only |
| Audio (raw) | ephemeral | ephemeral (ZDR) | never stored |

The MVP keeps everything on the cloud server for speed; the privacy target
(memory on-device, patterns-only sync, ephemeral audio) is a migration, not a
redesign, because the memory is already an isolated data layer.

---

## 7. The pluggable brain (roadmap)

Because personality = data (§1), *where the thinking runs* is a swappable
backend while the memory file stays constant:

```
v1  cloud brain (disciplined: Bedrock + Zero-Data-Retention)   ← ship fast, low friction
v2  parent's phone (on-device small model; memory syncs)       ← privacy tightens
v3  bundled dock (RK3588-class, ~$40) or eventually on-toy      ← fully local
```

**v1 has no home hub** — the brain is a *cloud server we run*. The **home hub**
first appears at v2 (the parent's phone) and v3 (a bundled ~$40 dock); the
family never buys a Mac mini. Same memory file, same product, no re-architecture
— see `LOCAL-MODEL-HARDWARE.md` for the silicon roadmap (RK3668/3688 bandwidth
wave, Hailo-10H, BitNet 1-bit models) that makes v3 a 2027–2028 event.

---

## 8. Privacy & safety architecture (target)

- **Patterns, not transcripts:** the dashboard reads derived aggregates; raw
  conversation and `PROFILE.md` stay on-device. The parent sees moods,
  milestones, topic flags — never words.
- **Guardrail layer** (Phase B/C, distinct from the system prompt): input/output
  moderation, age-conditioned topic policy, self-harm → gentle handoff + parent
  alert. FoloToy's prompt-only safety got it cut off; ours is a separate layer.
- **Compliance-by-design:** AI-status disclosure, break reminders (CA SB 243 /
  NY Art 47), no manipulation/engagement dark patterns (EU AI Act), COPPA
  consent + retention + deletion. On-device processing that never transmits is
  not "collection" under COPPA.
- **Never bricks:** family owns the memory; open-source firmware + local-server
  protocol (the OpenMoxie lesson).

### Parent dashboard & alerts (Phase 2; hooks built earlier)

The dashboard is the "for parents" half of the product: mood trends, milestones,
conversation starters, and **alerts**. Alerts are **two tiers — do not conflate:**

| Tier | Examples | Source | Surfacing |
|---|---|---|---|
| **Soft signal** (social/emotional) | "felt left out twice this week", went quiet about a friend, pre-test anxiety | background **reflection** loop (patterns over time) | gentle nudge + conversation starter, "worth a check-in" |
| **Serious safety** | self-harm, abuse, danger, being hurt | **guardrail layer** in real time (+ reflection) | urgent, prominent + crisis resources — **legally mandated** (CA SB 243 / NY Art 47) |

Both write to a per-child **alert feed** the dashboard reads. Design rules:

- **Patterns, not transcripts, even in alerts:** an alert = category + severity +
  suggested action + a starter, *not* the child's raw words. Parents get enough
  to act; confidences stay private. Serious-safety may reveal more — a
  deliberate, counsel-reviewed exception, not the default.
- **Child transparency (UK AADC):** if a parent can be alerted, the child must
  know that's possible; the companion tells the child when something is shared.
  Otherwise it's the surveillance tool the product promises it isn't.
- **Never a diagnosis:** "worth a gentle check-in," never "your child has X"
  (IL/TX/UT bar AI mental-health assessment framing).
- **Data flow:** pattern alerts derive from the fact store / reflections (no new
  data path); real-time safety flags come from the guardrail layer. The dashboard
  reads the derived alert feed + aggregates — never the episodic store.

---

## 9. Migration checklist (before any non-family child)

- [ ] TTS: ElevenLabs → a provider whose ToS permits under-13 (Cartesia / Bedrock).
- [ ] LLM: OpenAI → AWS Bedrock (the ToS-viable frontier path).
- [ ] Guardrail layer live + red-teamed against PIRG-style prompts.
- [ ] Memory moved on-device; only patterns sync (encrypted).
- [ ] COPPA: verifiable parental consent, retention policy, export/delete.
- [ ] Counsel review; monthly regulatory watch (AI-toy moratoria).

---

## 10. Directory layout

```
megotchi/
  megotchi_memory/      the moat — memory engine (built)
    store.py            episodic (append-only) + bi-temporal facts (SQLite)
    extract.py          Mem0-pattern extract → ADD/UPDATE(supersede)/soft-DELETE/NOOP
    reflect.py          nightly reflection + PROFILE.md
    retrieve.py         working-context builder (recency×importance)
    speech.py           speak() grammar-constrained reply + clean_speech() fallback
    llm.py              provider-agnostic: ollama / anthropic / openai
  voice/                the cascade (Phase A)
    stt.py  tts.py  pipeline.py  run_mac.py  server.py
  companion/run.py      text REPL (dogfood without audio)
  evals/                the moat metric — suite, cases, RESULTS.md
  ARCHITECTURE.md  README.md  FINDINGS.md  LOCAL-MODEL-HARDWARE.md
../MEGOTCHI-STRATEGY.md  (repo root)
```
