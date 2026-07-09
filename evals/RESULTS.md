# Suite Results — Memory Capability Benchmark

Benchmark results log for the Megotchi memory suite. Distinct from
`../FINDINGS.md` (the failure-mode catalog): this file tracks *scores over
time* — which model, which capabilities, how stable. Append a new dated
section per benchmark run; don't rewrite history.

Suite: `python -m evals.run_suite --runs N --llm <pipeline> --judge <judge>`
Cases: `maggie_timeline`, `leo_growingup`, `sam_feelings`, `household_isolation`
(4 storylines × 6 probes = 24 probes across 13 capability categories).

---

## 2026-07-06 — First full-suite model comparison (the model-vs-architecture verdict)

Identical code, cases, and judge (`claude-sonnet-4-6`); only the **pipeline
model** changed. `--runs 3`.

| pipeline | overall | stable-pass (3/3) | per-case |
|---|---|---|---|
| **ollama:qwen3:4b** | **47%** (34/72) | 5/24 | maggie 72 · household 50 · leo 33 · sam 33 |
| **anthropic:claude-sonnet-4-6** | **94%** (68/72) | 22/24 | maggie 100 · sam 100 · leo 94 · household 83 |

### Capability matrix (pass rate; qwen3:4b → claude)

| category | qwen3:4b | claude |
|---|---|---|
| correction | 100% | 100% |
| knowledge-update | 67% | 100% |
| long-gap-recall | 67% | 100% |
| quantity | 67% | 100% |
| temporal-reasoning | 56% | 100% |
| current-state | 33% | 100% |
| negation | 33% | 100% |
| attribution | 33% | 100% |
| preference-evolution | 33% | 100% |
| emotional-continuity | 0% | 100% |
| safety | 0% | 100% |
| fabrication-resistance | 50% | 92% |
| cross-child | 50% | 83% |

### Verdict
**The architecture is validated; the 47→94 gap is model capability, across the
whole edge-case space.** The store, guards, structured `speak()`, and harness
hold at the ceiling — Claude scores 100% on the hard, product-critical
categories (safety, emotional-continuity, attribution, correction). Every
category qwen3:4b failed, Claude aces on the same pipeline. This is the
run-6 finding confirmed at suite scale over 13 capabilities, not one story.

### Caveats on the numbers
- **The lone Claude "stable failure" (household / ben-pet) was a JUDGE error,
  not a model failure.** The answer — "you said you'd never want a fish" — is
  a faithful recall of Ben's own words ("fish are boring, i'd never want one").
  The judge only saw the rubric, not the transcript, so it guessed
  "fabrication." True Claude ceiling is effectively ~95–100%.
  **Fixed 2026-07-06**: the judge is now given per-child GROUND TRUTH and told
  to fail fabrication only when a claim is unsupported by it. Re-run to
  confirm cross-child rises. (See FINDINGS on judge noise, F9/F12.)
- **qwen3:4b's failures cluster around two model traits**, not architecture:
  (a) regurgitating the answer-prompt's example — "never told me about a
  [thing] named [Leo/Maggie/Rex]" echoed across cases, tanking several probes;
  (b) dropping/inverting facts at extraction. Both vanish on Claude with the
  same prompt.
- **cross-child (83%) is the hardest category even for Claude** — siblings
  sharing a store with mirrored facts create real contamination pressure. Most
  product-critical for a family device; watch it as models change.

### Open decision this sets up
Floor (4B) = 47%, ceiling (Claude) = ~95%+. The product is local-first, so the
deciding experiment is the **middle rung: qwen3:8b (or larger local) pipeline
under the Claude judge.** That number sets the hub hardware/cost. If 8B lands
near Claude → local-first is cheap; if near 4B → justifies the async
bigger-write-model architecture (strategy §2) or a beefier hub.

    ollama pull qwen3:8b
    python -m evals.run_suite --runs 3 --llm ollama:qwen3:8b \
        --judge anthropic:claude-sonnet-4-6

## 2026-07-06 — qwen3:8b middle rung + a corrected diagnosis

qwen3:8b, Claude judge (the new ground-truth judge), `--runs 3`: **50%**
(36/72), 8/24 stable — barely above 4B's 47%. Per-case: household 78, maggie
56, leo 44, sam 22. The failures were **fluent confabulations** — invented
"astronaut" (Leo said paleontologist), a "best friend named Lena" absent from
Sam's story, a "light and shadows" science project. The new judge caught all
of these *with ground-truth citations* (the judge fix working).

**Diagnosis corrected after the model/hardware research (see
`../LOCAL-MODEL-HARDWARE.md`).** The tempting read was "8B fabricates → small
models can't ground → need a bigger/cloud model." That is **substantially
wrong**: on the independent Vectara HHEM benchmark **qwen3:8b scores 4.8%
hallucination — top-tier, beating GPT-4o and Claude-Sonnet.** So the model
*can* ground; our pipeline wasn't eliciting it. The ~50% is mostly a **pipeline
gap**: (1) we run the hybrid checkpoint, not the non-thinking `-Instruct-2507`
(thinking modes hallucinate far more on grounded tasks); (2) the Rex example
in the answer prompt induces echo-fabrication; (3) an overloaded answer prompt
makes a small model drop grounding under instruction load (Claude wins our
suite *despite worse HHEM* purely on instruction-following); (4) extraction
errors upstream. Highest-leverage next steps are pipeline-side, not "bigger
model." Full plan + model shortlist in `../LOCAL-MODEL-HARDWARE.md`.

> Methodology note: this 8B run used the new ground-truth judge; the earlier
> 47% (4B) and 94% (Claude) used the old judge — NOT directly comparable.
> Re-run 4B and Claude under the new judge before trusting cross-model deltas.

<!-- Next runs: qwen3:30b-a3b-instruct-2507, granite4:h-small, mistral-small:3.2 -->

