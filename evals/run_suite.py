"""Megotchi memory test suite — run every storyline, N times, and report a
capability matrix so we see WHAT breaks, not just a single score.

For each case it ingests a fresh throwaway store and probes it, repeated
`--runs` times. A probe is:
  - stable-pass  : passed every run       (trustworthy)
  - flaky        : passed some, failed some (variance — the real risk)
  - stable-fail  : failed every run       (a genuine capability gap)

Reports: per-case scores, and a matrix of capability category -> pass rate,
so "temporal-reasoning is solid but attribution is flaky" falls straight out.

Usage (from megotchi/):
    python -m evals.run_suite                       # all cases, 1 run each
    python -m evals.run_suite --runs 3              # 3x each, stability
    python -m evals.run_suite --llm ollama:qwen3:4b --judge anthropic:claude-sonnet-4-6
    python -m evals.run_suite evals/cases/sam_feelings.json --runs 3 --verbose
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.run_eval import run_case
from megotchi_memory import LLM

CASES_DIR = Path(__file__).resolve().parent / "cases"


def _bar(rate: float, width: int = 12) -> str:
    filled = round(rate * width)
    return "█" * filled + "·" * (width - filled)


def main() -> None:
    ap = argparse.ArgumentParser(description="Megotchi memory test suite")
    ap.add_argument("cases", nargs="*",
                    help="case files (default: all in evals/cases/)")
    ap.add_argument("--llm", default=None, help="pipeline model spec")
    ap.add_argument("--judge", default=None, help="judge model spec")
    ap.add_argument("--runs", type=int, default=1, help="repeats per case")
    ap.add_argument("--verbose", action="store_true", help="print each answer")
    args = ap.parse_args()

    paths = [Path(c) for c in args.cases] or sorted(CASES_DIR.glob("*.json"))
    if not paths:
        print("No case files found."); sys.exit(1)

    llm = LLM(args.llm)
    judge = LLM(args.judge or args.llm)
    print(f"Suite: {len(paths)} case(s) x {args.runs} run(s) | "
          f"pipeline={llm.provider}:{llm.model} judge={judge.provider}:{judge.model}\n")

    # probe key -> list of verdict bools across runs; plus metadata
    probe_runs: dict[tuple[str, str], list[bool]] = defaultdict(list)
    probe_meta: dict[tuple[str, str], dict] = {}
    cat_runs: dict[str, list[bool]] = defaultdict(list)
    case_scores: dict[str, list[float]] = defaultdict(list)

    errors = 0
    for path in paths:
        case = json.loads(path.read_text())
        name = path.stem
        for run_i in range(args.runs):
            try:
                results = run_case(case, llm, judge, quiet=True)
            except Exception as e:  # transient API/network failure post-retry
                errors += 1
                print(f"  {name} run {run_i + 1}/{args.runs}: SKIPPED "
                      f"({type(e).__name__}: {str(e)[:80]})")
                continue
            passed = sum(1 for r in results if r["verdict"] == "PASS")
            case_scores[name].append(passed / len(results) if results else 0.0)
            for r in results:
                key = (name, r["id"])
                ok = r["verdict"] == "PASS"
                probe_runs[key].append(ok)
                probe_meta[key] = r
                cat_runs[r["category"]].append(ok)
            print(f"  {name} run {run_i + 1}/{args.runs}: {passed}/{len(results)}"
                  + ("" if not args.verbose else ""))
            if args.verbose:
                for r in results:
                    mark = {"PASS": "✓", "FAIL": "✗"}.get(r["verdict"], "?")
                    print(f"      {mark} [{r['category']}] {r['id']}: {r['answer'][:90]}")

    # ---- capability matrix ----
    print("\n" + "=" * 60)
    print("CAPABILITY MATRIX  (pass rate across all runs)")
    print("=" * 60)
    for cat in sorted(cat_runs, key=lambda c: sum(cat_runs[c]) / len(cat_runs[c])):
        runs = cat_runs[cat]
        rate = sum(runs) / len(runs)
        print(f"  {cat:22s} {_bar(rate)} {rate*100:3.0f}%  ({sum(runs)}/{len(runs)})")

    # ---- flaky / failing probes ----
    stable_fail = []
    flaky = []
    for key, runs in probe_runs.items():
        if not any(runs):
            stable_fail.append(key)
        elif not all(runs):
            flaky.append(key)

    if stable_fail:
        print("\nSTABLE FAILURES (failed every run — real gaps):")
        for key in stable_fail:
            m = probe_meta[key]
            print(f"  ✗ {key[0]} / {key[1]} [{m['category']}]")
            print(f"      Q: {m['question']}")
            print(f"      A: {m['answer'][:120]}")
            print(f"      why: {m['note']}")
    if flaky:
        print("\nFLAKY (passed some runs, failed others — variance risk):")
        for key in flaky:
            runs = probe_runs[key]
            m = probe_meta[key]
            print(f"  ~ {key[0]} / {key[1]} [{m['category']}]  "
                  f"{sum(runs)}/{len(runs)} passed")

    # ---- per-case + overall ----
    print("\n" + "=" * 60)
    print("PER-CASE  (avg pass rate)")
    print("=" * 60)
    for name in sorted(case_scores):
        scores = case_scores[name]
        avg = sum(scores) / len(scores)
        print(f"  {name:24s} {_bar(avg)} {avg*100:3.0f}%")

    all_runs = [ok for runs in probe_runs.values() for ok in runs]
    overall = sum(all_runs) / len(all_runs) if all_runs else 0.0
    stable = sum(1 for runs in probe_runs.values() if all(runs))
    print(f"\nOverall pass rate: {overall*100:.0f}%  "
          f"({sum(all_runs)}/{len(all_runs)} probe-runs)")
    if args.runs > 1:
        print(f"Stable-pass probes: {stable}/{len(probe_runs)}  "
              f"(passed all {args.runs} runs)")
    if errors:
        print(f"\n⚠  {errors} run(s) skipped on transient errors — results are "
              f"partial. Re-run for a complete matrix.")


if __name__ == "__main__":
    main()
