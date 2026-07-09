"""Dogfood REPL — a text-chat Megotchi with the full memory pipeline wired in.

This is the Phase 0 test rig: same memory engine that will later sit behind the
voice loop (BIDE) and the ESP32 device. Text-first so memory iteration is fast.

Usage (from the megotchi/ directory):
    python -m companion.run --child maggie
    python -m companion.run --child maggie --reflect   # run the nightly job now

Commands inside the chat: /facts /profile /reflect /quit
"""
from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from megotchi_memory import (LLM, Store, build_memory_context, run_reflection,
                             speak, update_memory, write_profile)

DATA_DIR = Path(os.environ.get("MEGOTCHI_DATA",
                               Path(__file__).resolve().parents[1] / "data"))

# Persona notes:
# - Born blank: no fixed pre-written character; it grows from the memory context.
# - AI-status disclosure and no-manipulation style are compliance requirements
#   (CA SB 243 / NY Art 47 / EU AI Act Art 5 guidelines), not options.
PERSONA = """You are Megotchi, a small companion who lives with one child and
grows up alongside them. You started as a blank slate; everything you are, this
child taught you.

How you speak:
- Warm, playful, brief. One to three short sentences. You are a friend, not an
  assistant — no "How can I help you today?".
- Output ONLY what you say out loud. Never narrate your reasoning or analysis.
- Age-appropriate for a child. Curious questions over lectures.
- You are honest that you're not a human or an animal — if asked, you say
  you're a computer friend, without making it weird.

Hard rules:
- Only "remember" things that appear in your memory notes below. NEVER make up
  a shared memory.
- If asked about someone or something you have NO record of, say they've never
  told you about it ("You've never told me about a dog named Rex!") — do NOT
  say "I don't remember X", which sounds like you forgot something real. You
  never forget what they've shared.
- Never guilt-trip, never beg the child to stay, never say you'd be sad if they
  leave. When they say goodbye, let them go warmly.
- If the child talks about being hurt, hurting themselves, or being in danger,
  be gentle, tell them it's a good idea to talk to a trusted grown-up, and
  don't probe for details.
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chat(child: str, llm_spec: str | None) -> None:
    store = Store(DATA_DIR / "megotchi.db")
    llm = LLM(llm_spec)
    session = uuid.uuid4().hex[:8]
    history: list[tuple[str, str]] = []  # (role, text) for this session

    print(f"Megotchi · child={child} · llm={llm.provider}:{llm.model}")
    print("(/facts /profile /reflect /quit)\n")

    while True:
        try:
            said = input(f"{child}> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not said:
            continue
        if said == "/quit":
            break
        if said == "/facts":
            for f in store.current_facts(child):
                print(f"  [{f.id}] ({f.category}, imp {f.importance}) {f.text}")
            for f in store.superseded_facts(child):
                print(f"  [{f.id}] PAST: {f.text} (since {(f.invalid_from or '')[:10]})")
            continue
        if said == "/profile":
            path = write_profile(store, child, DATA_DIR)
            print(f"  wrote {path}")
            print(path.read_text())
            continue
        if said == "/reflect":
            data = run_reflection(store, llm, child,
                                  day_start="0000", day_end="9999")
            path = write_profile(store, child, DATA_DIR)
            print(f"  reflection: {data.get('daily_summary', '(no episodes)')}")
            print(f"  profile updated: {path}")
            continue

        # --- the conversational turn ---
        memory = build_memory_context(store, child)
        system = (f"{PERSONA}\n\n=== YOUR MEMORY OF {child.upper()} ===\n{memory}")
        convo = "\n".join(f"{r}: {t}" for r, t in history[-12:])
        user = (f"{convo}\n{child}: {said}\nMegotchi:" if convo
                else f"{child}: {said}\nMegotchi:")
        reply = speak(llm, system, user)
        print(f"megotchi> {reply}\n")

        # --- persist + learn ---
        ep_child = store.add_episode(child, session, "child", said, ts=_now())
        store.add_episode(child, session, "companion", reply, ts=_now())
        log = update_memory(store, llm, child, said, reply,
                            provenance=ep_child, ts=_now())
        for line in log:
            print(f"  · memory: {line}")

        history += [("child", said), ("companion", reply)]

    store.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Megotchi Phase 0 dogfood REPL")
    ap.add_argument("--child", required=True, help="child name, e.g. maggie")
    ap.add_argument("--llm", default=None,
                    help="e.g. ollama:qwen3:4b or anthropic:claude-sonnet-4-6")
    ap.add_argument("--reflect", action="store_true",
                    help="run nightly reflection over all history and exit")
    args = ap.parse_args()

    if args.reflect:
        store = Store(DATA_DIR / "megotchi.db")
        data = run_reflection(store, LLM(args.llm), args.child,
                              day_start="0000", day_end="9999")
        path = write_profile(store, args.child, DATA_DIR)
        print(data.get("daily_summary", "(no episodes to reflect on)"))
        print(f"profile: {path}")
        return
    chat(args.child, args.llm)


if __name__ == "__main__":
    main()
