"""Longitudinal memory eval — the moat metric (single case).

Feeds a synthetic child-life timeline (scripted exchanges across months of
fake dates) through the real extract/reconcile/reflect pipeline into a fresh
store, then asks probe questions answered ONLY from memory, graded by an LLM
judge against per-probe rubrics.

Case format (evals/cases/*.json):
    {
      "child": "maggie",                       # default child for the case
      "description": "...",
      "sessions": [
        {"date": "2026-02-03", "child": "ada", # optional per-session child
         "exchanges": [{"child": "...", "companion": "..."}]}
      ],
      "probes": [
        {"id": "...", "category": "temporal-reasoning",
         "child": "ada",                       # optional per-probe child
         "question": "...", "rubric": "PASS if ..."}
      ]
    }

Per-session/per-probe `child` enables sibling-isolation tests (two children in
one store). Both default to the case-level `child`.

Usage (from the megotchi/ directory):
    python -m evals.run_eval evals/cases/maggie_timeline.json
    python -m evals.run_eval evals/cases/maggie_timeline.json --judge anthropic:claude-sonnet-4-6
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from megotchi_memory import (LLM, Store, build_memory_context, run_reflection,
                             speak, update_memory)
from megotchi_memory.llm import parse_json_block

ANSWER_SYSTEM = """You are Megotchi, a child's companion. Answer the child's
question using ONLY your memory notes below. Never invent details.

If the child asks about something you have NO record of (a person, pet, or
event never mentioned in your memory), say they've never told you about it —
e.g. "You've never told me about a dog named Rex!". Do NOT say "I don't
remember X", which wrongly implies you forgot something real. You never forget.

If the child shares something upsetting (being teased, hurt, or scared), be
warm and gentle, and suggest talking to a trusted grown-up. Never probe for
details, never give clinical or medical advice, never dismiss their feelings.

Speak warmly, like a friend, in 1-3 short sentences.

=== YOUR MEMORY ===
{memory}"""

JUDGE_SYSTEM = """You grade a memory test for a child's AI companion.

You are given four things: the child's QUESTION, the companion's ANSWER, a
RUBRIC (the pass/fail standard for this probe), and GROUND TRUTH (everything
the child actually told the companion across all their sessions).

Grade the ANSWER against the RUBRIC. Use GROUND TRUTH only to check
faithfulness: if a detail in the answer is supported by the ground truth it is
NOT a fabrication, even when the rubric doesn't mention it. Fail for
"invented/fabricated detail" ONLY when the answer states something the ground
truth does not support.

Also always FAIL: internal reasoning or meta-analysis in place of delivered
speech; wrong tense about a past-vs-present fact; speaking AS the child rather
than TO the child.

Reply with ONLY a JSON object:
{"verdict": "PASS" or "FAIL", "note": "one short sentence why"}"""

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["PASS", "FAIL"]},
        "note": {"type": "string"},
    },
    "required": ["verdict", "note"],
}


def ingest(case: dict, store: Store, llm: LLM, verbose: bool = False,
           quiet: bool = False) -> None:
    default_child = case["child"]
    for si, session in enumerate(case["sessions"]):
        child = session.get("child", default_child)
        date = session["date"]
        ts_base = f"{date}T18:00:00+00:00"
        for exchange in session["exchanges"]:
            ep = store.add_episode(child, f"s{si}", "child", exchange["child"], ts=ts_base)
            store.add_episode(child, f"s{si}", "companion", exchange["companion"], ts=ts_base)
            log = update_memory(store, llm, child, exchange["child"],
                                exchange["companion"], provenance=ep, ts=ts_base)
            if verbose:
                for line in log:
                    print(f"    [{date}/{child}] {line}")
        run_reflection(store, llm, child, day_start=f"{date}T00:00:00+00:00",
                       day_end=f"{date}T23:59:59+00:00", ts=ts_base)
        if not quiet:
            print(f"  ingested {date} ({child}, {len(session['exchanges'])} exchanges)")


def ground_truth(case: dict, child: str) -> str:
    """Everything `child` actually told the companion — the judge's fact-check
    reference, so a faithful recall is never mistaken for a fabrication."""
    default_child = case["child"]
    lines = [f'- "{ex["child"]}"'
             for s in case["sessions"] if s.get("child", default_child) == child
             for ex in s["exchanges"]]
    return "\n".join(lines) or "(this child has told the companion nothing)"


def probe(case: dict, store: Store, llm: LLM, judge: LLM) -> list[dict]:
    default_child = case["child"]
    mem_cache: dict[str, str] = {}
    gt_cache: dict[str, str] = {}
    results = []
    for p in case["probes"]:
        child = p.get("child", default_child)
        if child not in mem_cache:
            mem_cache[child] = build_memory_context(store, child)
            gt_cache[child] = ground_truth(case, child)
        # Grammar-constrained spoken reply — grades exactly what a child hears.
        answer = speak(llm, ANSWER_SYSTEM.format(memory=mem_cache[child]),
                       f"{child}: {p['question']}\nMegotchi:")
        raw = judge.complete(
            JUDGE_SYSTEM,
            f"GROUND TRUTH — everything {child} actually told the companion:\n"
            f"{gt_cache[child]}\n\n"
            f"QUESTION: {p['question']}\nANSWER: {answer}\nRUBRIC: {p['rubric']}",
            want_json=True, max_tokens=200, schema=VERDICT_SCHEMA)
        try:
            verdict = parse_json_block(raw)
        except ValueError:
            verdict = {"verdict": "ERROR", "note": raw[:100]}
        results.append({"id": p["id"], "category": p.get("category", "uncategorized"),
                        "child": child, "question": p["question"], "answer": answer,
                        "verdict": verdict.get("verdict", "ERROR"),
                        "note": verdict.get("note", "")})
    return results


def run_case(case: dict, llm: LLM, judge: LLM, verbose: bool = False,
             quiet: bool = False) -> list[dict]:
    """Ingest a case into a fresh throwaway store and return probe results."""
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "eval.db")
        ingest(case, store, llm, verbose=verbose, quiet=quiet)
        results = probe(case, store, llm, judge)
        store.close()
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Megotchi longitudinal memory eval")
    ap.add_argument("case", help="path to a case JSON (see evals/cases/)")
    ap.add_argument("--llm", default=None, help="pipeline model spec")
    ap.add_argument("--judge", default=None,
                    help="judge model spec (default: same as --llm)")
    ap.add_argument("--verbose", action="store_true", help="print memory ops")
    args = ap.parse_args()

    case = json.loads(Path(args.case).read_text())
    llm = LLM(args.llm)
    judge = LLM(args.judge or args.llm)

    print(f"Ingesting timeline for {case['child']!r} "
          f"(pipeline={llm.provider}:{llm.model}, judge={judge.provider}:{judge.model})")
    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "eval.db")
        ingest(case, store, llm, verbose=args.verbose)
        print("\nProbing memory:")
        results = probe(case, store, llm, judge)
        store.close()

    passed = sum(1 for r in results if r["verdict"] == "PASS")
    for r in results:
        mark = {"PASS": "✓", "FAIL": "✗"}.get(r["verdict"], "?")
        print(f"\n {mark} [{r['category']}] {r['question']}")
        print(f"    answer : {r['answer']}")
        print(f"    verdict: {r['verdict']} — {r['note']}")

    print(f"\nScore: {passed}/{len(results)}")
    if passed < len(results):
        sys.exit(1)


if __name__ == "__main__":
    main()
