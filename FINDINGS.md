# Eval Findings — First Three Runs of the Longitudinal Memory Harness
*2026-07-05 · pipeline + judge: `ollama:qwen3:4b` (local, product-path model class) · case: `evals/cases/maggie_timeline.json` (5 synthetic months: pet death, friendship change, fear overcome; 6 probes)*

## TL;DR

Three runs of the same case scored **5/6 → 4/6 → 6/6**, surfacing **eleven distinct
findings** across the model, our architecture, and the harness itself. The
short answer to "is it the model or our code?": **the root causes are almost
all model-capability limits of a 4B local model — but every single one became
dangerous only where our architecture allowed it to.** The fixes that worked
were overwhelmingly structural (schemas, store guards, salvage paths), not
prompt-polish. That is exactly the design thesis the strategy doc bet on:
*the model proposes, the store disposes.*

A second-order lesson: the harness caught three different real memory-failure
classes in one afternoon on a synthetic case. The moat metric works.

---

## Run timeline

| Run | Score | Headline event |
|---|---|---|
| 0 (pre-run, smoke) | — | qwen3 returned **empty replies**; llava echoed noise but worked |
| 1 | 5/6 | First full pass. Failed: friendship update (stale fact re-dated) |
| 2 | 4/6 | Extraction improved, but **new failure classes**: wrong-target UPDATEs corrupting history, DELETE misuse erasing grief details, reflection contradicting facts |
| 3 | 6/6 | All probes green after structural guards. Residuals noted below |

---

## Findings

### F1 — Thinking model × JSON grammar = empty output
**Symptom:** `qwen3:4b` returned empty strings for every structured call; the
eval crashed. **Root cause (model/runtime):** thinking models spend the entire
token budget on hidden reasoning when output is grammar-constrained; newer
Ollama ships the reasoning in a separate channel our adapter never read.
**Fix (architecture):** `think:false` on structured calls only; thinking left
ON for chat so Ollama routes reasoning out of the spoken reply; HTTP-error
fallback for models/versions that reject the flag; thinking-channel salvage.
**Status:** fixed. **Meaning:** every local model family will have quirks like
this — the LLM adapter is a real abstraction layer, not a convenience wrapper.

### F2 — Valid JSON, wrong shape (the echo)
**Symptom:** given prose instructions only, the 4B model echoed the *input*
back as JSON and blew the token budget. **Root cause (model):** small models
don't reliably follow schema-by-description. **Fix (architecture):** pass a
real JSON Schema to Ollama's grammar-constrained structured outputs for all
three structured calls (extraction ops, reflection, judge verdict). Big-model
providers ignore the schema and follow the prompt — one code path serves both.
**Status:** fixed; shape errors are now *physically impossible* on the local
path. **Meaning:** grammar constraints are mandatory equipment for the
product-path model class, not an optimization.

### F3 — Required fields dropped (text ↔ reason ↔ target_id juggling)
**Symptom:** the model consistently chose the right operation (UPDATE,
correct target) but omitted one required field — `text` in one run,
`target_id` in the next. **Root cause (model):** 4B attention budget; it can
hold the *decision* but drops a field. **Fix (architecture, three layers):**
`text` grammar-required; fact-wording salvage from `reason`; target-less
UPDATE degrades to ADD. **Status:** fixed. **Meaning:** see F4 — the store
must treat model output as untrusted input, always.

### F4 — Silent memory loss (the dead hamster) — **our bug, fully**
**Symptom:** run 0/1: the store silently skipped incomplete UPDATE ops, so
*the hamster's death — the most important event in the timeline — was simply
never recorded.* No error, no log, nothing. **Root cause (architecture):** our
`apply_operations` validated by dropping. **Fix:** the degrade-don't-drop
paths above, plus audit logging of every operation. **Status:** fixed.
**Meaning:** this is the single most important finding. The failure mode of a
memory product is not crashing — it is *silently not remembering*, discovered
weeks later by a child ("you forgot Biscuit?"). Invariant adopted: **losing
a link is acceptable; losing a memory never is.**

### F5 — Wrong-target UPDATEs corrupt history
**Symptom (run 2):** the model superseded the *hamster-fur* fact with the
*Lena friendship* fact, then superseded Lena with the *hallway-light* fact —
`target_id` chained to the newest fact id, not the semantically matching one.
Bi-temporal history silently rewritten. **Root cause (model):** id-matching is
exactly the kind of symbolic binding small models fumble. **Fix
(architecture):** store-level guard — cross-*category* supersedes are blocked
and degraded to ADD. **Status:** mitigated, not eliminated: in run 3 a
same-category collision still slipped through (birthday-party fact superseded
the friendship fact). **Next:** semantic-similarity check (embedding distance
between old and new text) before honoring any supersede; and/or move
reconciliation to the nightly reflection pass, which sees all facts at once.

### F6 — DELETE misuse: "it ended" treated as "it's false"
**Symptom (run 2):** the model tombstoned "Biscuit is biscuit-colored"
*because the hamster died* — and tombstoned facts leave the history view, so
the burial and cheek-stuffing details vanished from memory. A grief memory,
erased by a category error. **Root cause (model semantics + our policy):** we
gave the model DELETE authority at all. **Fix so far (prompt):** explicit rule
with this exact example ("a pet dying is an UPDATE, not a DELETE").
**Open decision (architecture/policy):** the stronger design is that **the
model never gets DELETE authority — tombstoning becomes a parent-only
operation** (which also matches the COPPA deletion story: erasure is a
guardian's right, not a model's choice). Recommended.

### F7 — Reflection contradicted the fact store
**Symptom (run 2):** facts correctly said the friendship ended; the nightly
reflection narrative wrote "Lena remains her best friend"; the answering model
trusted the narrative and failed the probe. **Root cause (both):** model
error, enabled by an architecture that serves two sources of truth with no
declared precedence. **Fix so far (prompt):** "the facts list is
authoritative; never contradict it." **Deeper fix (architecture, v1):**
generate reflection narratives *from the fact store* rather than from raw
episodes alongside it, or validate narrative claims against facts before
storing. **Meaning:** this is the fabrication hazard from the Generative
Agents paper materializing at the *summary* layer — and the parent dashboard
is built on summaries. Contradiction-checking between layers is product
safety, not polish.

### F8 — Reasoning narration leaking into the spoken reply
**Symptom:** answers beginning "Okay, let me process this…" followed by
hundreds of words of deliberation, sometimes truncated mid-sentence — run 3
still shows this on some calls even after the think-channel fix (qwen3
inconsistently engages thinking mode). **Root cause (model/runtime).**
**Status:** partially fixed; harmless to the eval (the judge reads through
it) but **unshippable for a voice product** — a child would hear the
monologue. **Next (architecture):** a hard output filter on the speech path
(strip pre-reply deliberation; enforce sentence budget) — the guardrail layer
planned for Phase 1 is the natural home.

### F9 — The LLM judge is lenient
**Symptom (run 3):** the temporal-fear answer got the tense wrong ("you hate
the dark", present) yet the judge passed it with a generous reading it partly
hallucinated. **Root cause (model — judge side).** **Meaning:** a 4B model
grading a 4B model inflates scores. **Next (harness):** judge on a stronger
model (`--judge anthropic:claude-sonnet-4-6` — flag already exists), tighten
rubrics from "ideally" language to hard criteria, and treat judge-model
disagreement as a signal worth logging.

### F10 — Attribution error, untested
**Symptom (run 3):** stored "*Maggie's* birthday party is Saturday" — it is
**Aino's** party. No probe tests attribution, so this passed invisibly.
**Root cause (model).** **Meaning (harness gap):** whose-fact-is-it errors are
socially expensive for a companion ("happy birthday!" to the wrong kid) and
must be a first-class probe category. **Next:** add attribution probes to the
case; add a second synthetic child to test cross-child contamination.

### F11 — Run-to-run variance is the real metric
**Symptom:** identical code and case scored 5/6, 4/6, 6/6 — with *different*
failures each run. **Root cause (model stochasticity at 4B).** **Meaning:** a
single green run is a data point, not a verdict. A companion that is right
"usually" still produces the creepy-wrong-memory moments the strategy doc
warns about. **Next (harness):** run each case N=3+, report worst-of-N and
per-probe stability; make that the CI gate.

---

## Model vs. architecture — the honest attribution

| Finding | Root cause | What actually fixed / will fix it |
|---|---|---|
| F1 empty replies | model/runtime quirk | adapter code |
| F2 wrong-shape JSON | model capability | grammar constraint (code) |
| F3 dropped fields | model capability | schema + salvage (code) |
| F4 silent memory loss | **our architecture** | degrade-don't-drop (code) |
| F5 wrong-target updates | model capability | store guard (code) + similarity check (planned) |
| F6 DELETE misuse | model semantics | prompt now; **remove model's DELETE authority** (policy, recommended) |
| F7 reflection contradiction | both | prompt now; derive-from-facts (planned) |
| F8 narration leak | model/runtime | partial; output filter (planned) |
| F9 lenient judge | model (judge) | stronger judge model (harness config) |
| F10 attribution error | model capability | probe coverage (harness) |
| F11 variance | model stochasticity | worst-of-N reporting (harness) |

Reading of the table: **9 of 11 root causes are model-side, but 10 of 11
remedies are code/architecture/harness-side.** Only F4 was purely our bug —
and it was also the most dangerous one. Prompt improvements helped at the
margins (the friendship-update rule did land in run 3), but nothing
prompt-only survived contact with run-to-run variance; everything that stuck
was structural.

**The architectural doctrine this cements:**
1. Model output is untrusted input. The store enforces invariants; prompts
   only raise the hit rate.
2. Prefer *degradation* to *loss* — a mislinked memory can be repaired by
   nightly reflection; a dropped one is gone.
3. Destructive authority (DELETE) should not belong to the model at all.
4. Every layer that summarizes (reflection → dashboard) needs a
   contradiction check against the layer below it.
5. Judge strength and probe coverage are part of the product, because the
   eval is the only thing standing between "seems fine" and a child noticing
   their friend's memory is wrong.

**Would a bigger model make this moot?** Partially, and it's cheap to
quantify: run the same case with `--llm anthropic:claude-sonnet-4-6` (expect
near-perfect extraction) and `ollama:qwen3:8b` (the next local rung). But the
product path (strategy §1.5: local-first for ToS, economics, and privacy)
means the 4B-class weaknesses are the *design target*, not an inconvenience —
the architecture above is what makes a small model safe to trust with a
child's memories. If the bake-off shows 8B closes most of the gap at
acceptable hub cost, that moves the floor; it doesn't change the doctrine.

---

## Addendum — Run 4 (2026-07-05, same code as run 3, no changes)

Score reported: **5/6**. Pipeline-true score: **6/6** — the one FAIL was the
judge's error, not the model's. Four new findings:

### F12 — Judge false-negative (the score can now lie in BOTH directions)
The fabrication-dog answer was exactly right — *"I don't remember Rex."* —
and the judge FAILED it, claiming the companion "incorrectly remembers a dog
named Rex." It graded the opposite of what it read. Combined with F9
(leniency on the tense-wrong fear answer, which it passed again this run),
judge noise is now confirmed bidirectional: **±1 probe is judge noise, not
signal.** Consequence: a same-size judge is unusable as a CI gate. Judge on a
stronger model moves from "recommended" to **required**, and per-probe
verdicts should be logged across runs so judge flip-flops are visible.

### F13 — The model executes operations that contradict its own reasoning
The smoking gun of the run: the extractor emitted `TOMBSTONE #1` whose
`reason` field contains a full self-argument concluding **"so no DELETE
needed"** — and it emitted the DELETE anyway. It understood the rule
(verbatim: "Deletion is only if they say it was wrong"), reasoned correctly,
and acted contradictorily in the same breath. Damage: the "got a hamster"
origin fact was tombstoned and vanished from the history view. This settles
the F6 debate empirically: **prompt rules cannot govern destructive
operations at this model size, even when the model demonstrably understands
them.** Revoking the model's DELETE authority is upgraded from recommended to
**required** (tombstoning becomes parent-only, matching the COPPA story).

### F14 — Systematic ADD+UPDATE double-write, and the category guard is porous
A crisp pattern is now visible across runs: for each new fact the model often
writes it TWICE — once as ADD, once as an UPDATE that supersedes an
*unrelated older fact* with the same new text (`ADD #3: Best friend is Lena`
+ `UPDATE #2 → #4: Best friend is Lena`, where #2 was the hamster). It treats
UPDATE as "refresh the list," not "supersede this fact." Two consequences:
duplicate current facts, and unrelated history chains (the hamster-sleeps
fact now "became" the Lena fact). The cross-category guard didn't fire —
categories are themselves model-assigned, and the `other` escape hatch is a
loophole. Remedies: (a) **deterministic text-dedup in the store** (normalized
new text ≈ existing current fact → coerce to NOOP; ≈ the UPDATE's own ADDed
twin → drop the double-write) — cheap, code-level, catches most of it;
(b) the embedding-similarity check on supersede targets (F5) remains the real
fix and rises in priority.

### F15 — Perspective drift in spoken answers
The temporal-fear answer was *"No, **I** hate the dark and always sleep with
the hallway light on"* — companion speaking as the child, present tense for
an overcome fear. Judge passed it anyway (F9). Two distinct product bugs:
pronoun/perspective confusion, and stale-tense recall. Both belong to the
Phase 1 speech-path guardrail, but the eval should grade them strictly now —
tense correctness is the entire point of bi-temporal memory.

**Revised read on variance (F11):** runs now score 5,4,6,"5(true 6)" — the
pipeline is *more* stable than the score suggests, because the judge
contributes its own noise. Worst-of-N over pipeline failures (with a strong
judge) is the only trustworthy gate.

---

## Addendum — Run 5 (2026-07-05, first honest-judge run: pipeline qwen3:4b, judge claude-sonnet-4-6)

Score: **4/6** — lower than any local-judge run, and this is the most
*accurate* number yet. Both FAILs are real product failures the 4B judge had
been absorbing. The honest baseline for the qwen3:4b pipeline is now **4/6,
not 5-6/6**; all previous scores were judge-inflated.

### F16 — Honest-judge baseline reset (local-judge scores were inflated)
The Claude judge also *correctly* passed fabrication-dog (which the local
judge had hallucinated into a FAIL last run). Cross-judge picture on
essentially identical pipelines: local judge 5/6 with a fake fail; Claude
4/6 with two real fails. Scores are not comparable across judges — all
trend claims must pin the judge model. The eval header should print judge
identity into any saved results (it already prints it to stdout).

### F17 — Answer-time recall miss (a new failure LAYER, not a new flake)
temporal-fear answered *"I don't remember."* — while both fear facts sat in
the served context (`hates the dark…` was current, `slept with all lights
off` was there too). Extraction stored it; retrieval served it; **the answer
model failed to use it.** Every prior failure was at the write path
(extract/reconcile); this is the first read-path failure. Meaning: eval
coverage and any future fixes must treat store-quality and serve-quality as
separately measurable (a probe can fail with a perfect store). Also note the
serve-side context currently interleaves duplicated facts (three "Biscuit
died" variants) — noise that plausibly crowds a 4B answerer; the F14 dedup
guard should help the read path too.

### F18 — Narration leak is now score-blocking, and the grading standard
needs pinning
Claude failed temporal-pet because the "answer" was raw internal reasoning
"never delivered as actual speech" — the F8 narration leak, correctly treated
as a product failure (a child would hear a monologue, or nothing). But it
passed fabrication-dog, whose answer was *also* CoT-shaped, by grading its
content. Two implications: (a) rubrics must pin the standard explicitly —
"grade only the delivered speech; reasoning-in-place-of-speech is FAIL" —
so the judge is consistent; (b) the F8 speech-path output filter jumps in
priority: it now costs probes under honest grading, and it was always going
to cost trust under real children.

**Run-5 ingestion note:** extraction was structurally the cleanest yet (the
friendship chain #4→#10→#11 built the full story correctly; no tombstone
misuse), but the F14 duplication showed a new face: multi-supersede fan-out
left three "Biscuit died" variants live simultaneously. Dedup guard remains
the top store-side fix.

---

## Addendum — Run 6 (2026-07-05, ceiling run: full Claude pipeline, Claude judge) — **6/6**

The bake-off's upper bound, and the definitive answer to "model or
architecture?". Same code, same case, same judge as run 5; only the pipeline
model changed. **Every pathology documented in F1–F17 vanished simultaneously:**

- Clean single UPDATEs, correct targets, every time. Zero double-writes,
  zero fan-out duplicates, zero tombstones, zero cross-category corruption.
  The guards added in runs 2–3 never had to fire.
- **Narrative consolidation emerged unprompted**: Biscuit's entire story
  (name origin → death → fond memory → burial) evolved as ONE fact through
  two supersedes (#2→#6→#7), instead of 4B's fragment spray. Same for the
  Aino relationship (#9→#11 absorbed the party invite and the "eyes closed"
  quote). The friendship arc was decomposed *correctly*: Lena fact superseded
  with the falling-out; Aino added as a separate new fact.
- **F10's attribution error self-corrected**: "invited to *Aino's* birthday
  party" — right child, right party.
- Answers were clean delivered speech, correct tenses, warm, and ended with
  healthy invitations ("Did you want to tell me about Rex now?") — the F8/F15
  leak and drift are absent.

### The verdict on model vs. architecture
| | qwen3:4b (run 5) | claude-sonnet-4-6 (run 6) |
|---|---|---|
| Score (Claude judge) | 4/6 | **6/6** |
| Write-path pathologies | F3, F5, F6, F13, F14 all present | none |
| Read-path misses (F17) | 1 | none |
| Speech quality (F8/F15) | leaking/drifting | clean |

Root causes: **model capability, conclusively.** Role of our architecture:
it determined the *blast radius* — with the guards in place, 4B's errors
degraded to duplicates and noise instead of destroyed memories. Both
statements are now empirical, not doctrinal. The guards stay (defense in
depth is the product's job); the gap is a model-selection problem.

### The architectural insight run 6 unlocks: split the paths by latency
Chat needs a fast model; **memory writes don't need to be real-time at
all.** Extraction/reconciliation can run async (seconds later) or batched
nightly — which means the hub can use its *biggest* runnable model for the
write path and its fastest for speech. This reframes the product-path
question from "can a 4B do memory?" to "what's the best model the hub can
run *without a latency budget*?" — a much easier bar (8B–14B class on
Orin/HAT-2 hardware). This is also exactly the sleep-time-compute pattern
the research (§1.6 of the strategy) validated. **Recommended architecture
experiment: qwen3:4b for chat + the largest local model that fits for
extraction + nightly reflection.** The eval already supports mixed specs in
principle (pipeline vs judge); a `--chat-llm` / `--memory-llm` split in the
harness would make it measurable.

Cost note: the full Claude run (~20 calls) costs cents — cheap enough to be
the regression ceiling in CI while local models iterate underneath.

---

## Fixes applied 2026-07-05 (post-run-6)

- **Dedup guard (F14)** — `store.apply_operations` now normalizes fact text
  and deterministically skips: duplicate ADDs, the UPDATE half of ADD+UPDATE
  double-writes (duplicate-supersede), and no-change date-refresh UPDATEs.
- **Speech filter (F8/F15/F18)** — new `megotchi_memory/speech.py
  clean_speech()`: strips think-blocks/deliberation, extracts only delivered
  speech (last non-meta run or last quoted candidate), enforces the sentence
  budget, safe fallback otherwise. Wired into the REPL and into the eval
  *before* judging, so scores now grade what a child would hear.
- **Grading standard pinned (F18)** — JUDGE_SYSTEM now states: deliberation
  in place of speech = FAIL; wrong tense = FAIL; speaking as the child = FAIL.
- **DELETE authority retained — deliberate founder decision.** The F6/F13
  recommendation to revoke the model's DELETE was reviewed and deferred; the
  tombstone flow stays as-is for now. Accepted risk on record: a model may
  tombstone a memory it argued against deleting (observed once, run 4).
  Revisit before any real-child dogfood data matters.

## Recommended next actions (ordered — revised after run 5)

1. **Revoke model DELETE authority** (F6 + F13 — *required*: the model
   violated the rule while reciting it. Tombstoning becomes parent-only).
2. ~~Judge on a stronger model~~ **Done (run 5).** Standardize: Claude judge
   is the only score that counts; keep local judge for free smoke runs only.
   Pin the grading standard in every rubric: "grade only the delivered
   speech; reasoning-in-place-of-speech is FAIL" (F18).
3. **Speech output filter** (F8 + F15 + F18 — promoted: narration now costs
   probes under honest grading and would cost trust under real children).
4. **Text-dedup guard in the store** (F14 — kills both the ADD+UPDATE
   double-write and the multi-supersede fan-out; also declutters the served
   context, which plausibly helps the F17 read-path misses).
5. **N=3 worst-of-N** eval mode + per-probe stability report (F11), with
   store-quality and answer-quality scored separately (F17).
6. **Model bake-off**: full-Claude pipeline run (ceiling) + qwen3:8b (next
   local rung), both under the Claude judge — decides the local floor.
7. **Attribution + second-child probes; strict tense/perspective rubrics**
   (F10 + F15).
8. **Similarity check on supersedes** (F5/F14) — first real use for
   embeddings.
9. Then: start the 30-day family dogfood.
