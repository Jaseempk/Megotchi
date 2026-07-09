# Megotchi — Phase 0: The Soul

The memory engine and its test rig. See `../MEGOTCHI-STRATEGY.md` for the full
plan; this directory is **Phase 0**: *a companion that remembers your kids
across weeks and demonstrably gets better because of it.*

**`FINDINGS.md`** documents the first three eval runs (5/6 → 4/6 → 6/6), the
eleven failure classes they surfaced, the model-vs-architecture attribution,
and the resulting design doctrine. Read it before touching `extract.py` or
`store.py` — the guards in there exist because of specific observed failures.

## Layout

```
megotchi_memory/   the product asset — five-layer memory engine (pure Python, stdlib-only core)
  store.py           L2 episodic (append-only) + L3 bi-temporal facts (SQLite)
  extract.py         Mem0-pattern extract → ADD/UPDATE(supersede)/soft-DELETE/NOOP
  reflect.py         L4 nightly "sleep" reflection + L5 PROFILE.md (parent-readable memory)
  retrieve.py        L1 working context (recency×importance, importance floor ≥8 never decays)
  llm.py             provider-agnostic: ollama:<model> or anthropic:<model>
companion/         dogfood rig — text-chat REPL with the full pipeline wired in
evals/             the moat metric — longitudinal synthetic-child-life eval
data/              (gitignored) sqlite db + per-child memory/PROFILE.md
```

## Run it

Needs Python 3.12+. No installs for the core. Pick a brain:

```bash
# local (product path):
ollama pull qwen3:4b            # or any chat model you have
export MEGOTCHI_LLM=ollama:qwen3:4b

# or cloud (dev speed): pip install anthropic; export ANTHROPIC_API_KEY=...
export MEGOTCHI_LLM=anthropic:claude-sonnet-4-6
```

Talk to it (from this directory):

```bash
python -m companion.run --child maggie
```

Every exchange persists episodes and runs the extract/reconcile pass (you'll
see `· memory: ADD #3: …` lines). In-chat commands: `/facts` (current + past),
`/reflect` (run the nightly job now), `/profile` (write + show PROFILE.md),
`/quit`.

Run one storyline:

```bash
python -m evals.run_eval evals/cases/maggie_timeline.json --verbose
```

It replays a synthetic child life through the real pipeline into a throwaway
store, then grades probes with an LLM judge. Exit code 1 on any failure —
CI-able. Always judge with a strong model; a 4B judge is noisy in both
directions (see FINDINGS F9/F12):

```bash
python -m evals.run_eval evals/cases/maggie_timeline.json \
    --llm ollama:qwen3:4b --judge anthropic:claude-sonnet-4-6 --verbose
```

Run the whole suite + capability matrix (the moat metric):

```bash
python -m evals.run_suite --runs 3 --judge anthropic:claude-sonnet-4-6
```

This ingests every case in `evals/cases/`, `--runs` times each, and reports a
**capability matrix** (pass rate per memory skill, worst first), a
**stable-fail vs flaky** split (fails-every-run = real gap; flickers =
variance risk), and per-case scores. That's how we see *what kind* of memory
breaks, not just a number.

**Storylines and what each stresses** (each ~5 sessions, 6 probes):
| case | edge cases exercised |
|---|---|
| `maggie_timeline` | knowledge-update, temporal-reasoning, fabrication-resistance, current-state |
| `leo_growingup` | quantity (new sibling), attribution (Grandpa's dog), preference-evolution (dinosaurs→space), long-gap-recall |
| `sam_feelings` | emotional-continuity, negation, correction (self-fixed date), **safety** (gentle handling of teasing) |
| `household_isolation` | cross-child (two siblings, one store — memory must never bleed between them) |

Add a storyline by dropping a new `evals/cases/*.json` in — the suite finds it
automatically. Each probe needs `id`, `category`, `question`, `rubric`;
sessions and probes may set `child` to test sibling isolation.

## Design invariants (do not break casually)

- **Episodes are append-only.** Nothing ever deletes a conversation row except
  an explicit parent-initiated wipe (COPPA path, to be built in Phase 2).
- **Facts are superseded, never edited; DELETE is a tombstone.** "You used to
  be scared of the dark" must always be answerable.
- **Importance ≥ 8 never decays** (the hamster's death outlives ten thousand
  lunches).
- **Provenance on every fact.** The companion may only reference store-backed
  memories; the fabrication probes in the eval enforce this.
- **PROFILE.md is the single human-readable memory artifact** — the parental
  audit surface and, later, the deletion/review UI's source of truth.
- **The engine never imports anything provider-specific outside `llm.py`.**

## Phase 0 exit criteria (from the strategy doc)

- [ ] 30 days of real family use (founders' kids), diary-study notes kept
- [ ] Memory eval green, including fabrication probes
- [ ] At least one unprompted "it remembered X from three weeks ago" moment
- [ ] Decision recorded: build on Hindsight / Mem0 OSS / keep this custom core

## Next (not yet built, by design)

- Embedding-based relevance in `retrieve.py` (only needed when facts > ~60)
- Hindsight / Mem0 OSS bake-off against this baseline on the same eval
- LongMemEval temporal-reasoning + knowledge-updates splits wired into `evals/`
- Voice: plug this engine into `../vision-companion/companion_live.py`
- Nightly reflection as a real scheduled job (launchd) instead of `/reflect`
