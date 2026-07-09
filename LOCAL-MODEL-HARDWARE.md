# Local Model + Hardware for the Write/Answer Path
*Researched 2026-07-06. Triggered by the eval finding that qwen3:8b ≈ qwen3:4b (~50%) and, worse, **fabricates fluently** — inventing plausible names/events (see `evals/RESULTS.md`). Question: what self-hostable model actually fixes fabrication, and what runs it?*

## The reframe (corrected): our fabrication is mostly PIPELINE, not the model

The 8B eval result implied "get a better model." The independent hallucination
data says the model was **not** the main problem. On the **Vectara HHEM
leaderboard** (2026-05-11; strict "use ONLY the passage, don't infer" task —
almost exactly our grounded-answer job; hallucination %, lower is better):

| model | params | halluc % | answer % | license | notes |
|---|---|---|---|---|---|
| Phi-4 | 14B dense | 3.7 | 80.7 ⚠ | MIT | best grounding but low answer-rate + weak instr-following |
| Llama-3.3-70B | 70B dense | 4.1 | 99.5 | Llama (700M MAU) | large |
| Gemma-3-12B | 12B dense | 4.4 | 97.4 | Gemma terms | 12B beats the 27B (7.4%) |
| **Qwen3-8B** | 8B dense | **4.8** | 99.9 | Apache 2.0 | **the model we tested — grounds excellently in isolation** |
| **Mistral Small 3** | 24B dense | 5.1 | 97.9 | Apache 2.0 | ★ |
| **Granite-4.0-H-Small** | 32B MoE / 9B act | 5.2 | 100.0 | Apache 2.0 | ★ 100% answer; ships Granite Guardian gate |
| Qwen3-14B / 32B | dense | 5.4 / 5.9 | 99.9 | Apache 2.0 | |
| *GPT-4o (ref)* | — | *9.6* | — | — | Qwen3/Granite/Mistral-Small all beat it |
| *Claude-Sonnet-4 (ref)* | — | *10.3* | — | — | reasoning flagships score WORSE on strict grounding |
| DeepSeek-R1 | MoE | 11.3 | 97.0 | MIT | ⚠ reasoning → confabulates |
| Phi-4-mini / Ministral-3-8B-2512 | small | 23.5 / 21.7 | — | — | ⚠⚠ avoid for grounding |

**The paradox that rewrites our diagnosis:** qwen3:8b scores **4.8%** here —
top-tier — yet fabricated fluently in *our* eval (~50%). The model can ground;
our pipeline wasn't letting it. Likely causes, in order of suspicion:

1. **We tested the hybrid checkpoint, not the Instruct one.** `ollama pull
   qwen3:8b` is the thinking-hybrid; the dedicated **`-Instruct-2507`
   non-thinking** checkpoints materially improved format/grounding stability.
   And the field-wide finding: **reasoning/"thinking" modes hallucinate far
   more on grounded tasks** (DeepSeek R1 14.3% vs V3 3.9% on the same test).
2. **The Rex example in the answer prompt** — we watched it get echoed and
   induce fabrication across cases. That's prompt-induced, not the model.
3. **Overloaded answer prompt.** Our `speak()` asks for grounding + warmth +
   1-3 sentences + the "never told me" rule + perspective rules at once. A
   small model drops grounding under instruction load; Claude's 94% on our
   suite despite its *worse* HHEM (10.3%) is explained by superior
   instruction-following juggling all those constraints without dropping
   facts. Simplifying the prompt likely recovers a lot.
4. **Garbage-in from extraction.** A wrong extracted fact yields a
   faithful-but-wrong answer that scores as fabrication.

So the honest correction to `evals/RESULTS.md`: the 50→94 gap is **not** proof
that small models can't ground. It is substantially a pipeline gap. Granite/
Mistral-Small are still worth adopting (100% answer rate, purpose-built for
RAG, ship faithfulness gates), but **the highest-leverage fixes may be
pipeline-side**, and we should test them before concluding we need a bigger
model or the cloud.

## License filter (decisive for a shippable product)
- ✅ **Apache 2.0 — ship freely:** all Mistral Small 3/3.1/3.2 (24B), all
  Granite 4.0/4.1, all OLMo.
- ✅ **NVIDIA Open Model License (commercial-ok):** all Nemotron.
- ❌ **Non-commercial — cannot self-host in the product:** all Cohere
  Command R/R+/R7B/Command-A (CC-BY-NC); Ministral 3B/8B `2410` (research
  license). So Cohere's citation-native RAG family is out despite being a
  technical fit — only Command A+ is commercial, and it's 218B with no
  published grounding numbers.

## Mapping to our two jobs
1. **Memory extraction/reconciliation (JSON ops)** — needs instruction-
   following + reliable structured output. Best: **Nemotron-Nano-9B-v2**
   (IFEval 90.3, BFCL 66.9, `/no_think` for clean JSON) or **Granite-4.0-
   H-Micro/Small**. We already grammar-constrain output, which de-risks any
   choice here.
2. **Grounded answering (low fabrication — the hard one)** — best HHEM:
   **Granite-4.0-H-Small (5.2%)** or **Mistral Small 3.2** (test our own HHEM).
   Consider an external faithfulness gate (Granite Guardian) on top.

**Single model for both, cleanest license: Granite 4.0** — H-Small for
quality, H-Micro (3B dense) for an edge fallback. Hybrid Mamba-2/Transformer
→ >70% less memory, ~2× faster than a comparable transformer, 512K context in
4.1.

## The hardware convergence (why this is lucky)

Home-hub inference is **memory-bandwidth-bound**, not compute-bound. That
kills dense big models on cheap-memory boxes (DGX Spark / Strix Halo run dense
32–70B at only 5–11 tok/s) — but it *rewards MoE*, where only active params
compute. **Granite-4.0-H-Small is a 9B-active MoE**: it needs only ~20–22 GB
at Q4 yet generates at ~9B speed. The best low-fabrication model is also the
ideal hardware shape. Candidate hubs to run it:

| hub | mem / BW | price | idle/load W | fit for H-Small (32B MoE, ~20GB Q4) |
|---|---|---|---|---|
| **Mac mini M4 Pro 48–64GB** | 273 GB/s | ~$1,800–2,400 | 3.5–7 / 30–40 | ✅ fast (MoE flies on MLX), silent, ~$14/yr power — **best home fit** |
| **Mac Studio M4 Max 64GB** | 546 GB/s | ~$2.5–3K | 5–8 / 40–80 | ✅ more dense headroom too (32B dense ~30–40 tg) |
| **Strix Halo 128GB** (Framework Desktop) | 215 GB/s | ~$2,000 | 8–14 / ~120 | ✅ + room for GPT-OSS-120B-class MoE later (34–56 tg); Linux+llama.cpp |
| RTX 4090/5090 (24–32GB) | 1,008–1,792 | ~$1.6–2.5K | 20–30 / 450–575 | fast but 450W+, full tower — wrong shape for a silent home box |
| DGX Spark 128GB | 273 GB/s | $3,999 | 10–30 / ~150 | ⚠ dense-slow; only worth it for large-MoE + long-context prefill |

Serving: **MLX on Apple** (fastest there), **llama.cpp Vulkan on Strix Halo**,
Ollama as the easy ops wrapper. Quantize at **Q4_K_M** (≈97–99% of FP16;
prefer a bigger model at Q4 over a smaller one at Q8). Quantize the KV cache
for long memory-extraction contexts.

## Architecture rules (load-bearing — likely bigger levers than model choice)
From independent 2026 structured-output research:
1. **Constrained decoding fixes JSON *validity*, not value *correctness*.**
   Grammar takes validity to ~96–100% but value accuracy caps ~80% — schema-
   perfect JSON with hallucinated leaf values. Still need a grounding gate.
2. **Tool-suppression trap:** applying a schema grammar to the same decode
   pass that decides an action can silently zero out that action. Keep
   decision and formatting in separate passes.
3. **Run non-thinking / Instruct checkpoints** for both jobs — reasoning
   traces are the dominant hallucination driver on grounded tasks.
4. **Add an external faithfulness gate** — Granite Guardian or Vectara
   HHEM-2.1-Open as an LLM-judge — to catch the residual ~5% before a child
   hears it. (This is also a product-safety control, not just a quality one.)

## Recommendation (two-pronged — fix pipeline AND test grounded models)

**A. Pipeline fixes first (cheap, may recover most of the gap on the model we
already have):**
- Test qwen3 **`-Instruct-2507`** non-thinking checkpoints, not the hybrid
  (`qwen3:30b-a3b-instruct-2507`, `qwen3:14b`).
- Remove the Rex example from the answer prompt; slim the answer prompt to
  grounding + brevity, move perspective/"never told me" handling to lighter
  touches or the faithfulness gate.
- Add a faithfulness gate on generated answers.

**B. Then A/B grounded models on our suite, under the Claude judge:**
`granite4:h-small` (best independent grounding + 100% answer + built-in gate) ·
`mistral-small:3.2` · `qwen3:30b-a3b-instruct-2507`. HHEM is a proxy; our
`run_suite` is the decision.

**C. Outcomes:**
- If a candidate + the pipeline fixes land ≥~85% on our suite → local-first is
  real; hub = **Mac mini M4 Pro 48–64GB** (silent, ~35W, ~$2K). No cloud for
  the write/answer path → privacy + unit-economics story holds intact.
- If not → the strategy's async design (big model does extraction/reflection
  offline; small local model only for low-stakes chat) or cloud-for-writes.

## Edge-silicon roadmap — "brain in the dock/toy" (researched 2026-07-06)

The Mac-mini hub is a red herring; a **bundled cheap Rockchip dock** is the real
low-friction local answer. Physics first: on-device decode is
**bandwidth-bound**, `tok/s ≈ memory-bandwidth ÷ model-GB` — TOPS is marketing.

**Shippable now (the v1 local brain):**
- **RK3588** (Orange Pi 5 / Radxa Rock 5C, **$70-100**, ~$30-50 board at volume):
  the only cheap SoC with a mature NPU-LLM runtime (RKLLM). Qwen2.5-3B INT4
  **~8-10 tok/s @ ~5-6W**; 1.5B ~17; 1.1B ~24. Pair with **sqlite-vec + MiniLM**
  RAG (retrieval ≈ free). **Bundle it into the charging dock → private, local,
  zero extra purchase, no Mac mini.** (Home-tethered; wall-powered.)
- **Hailo-10H** — the only ~2.5W LLM accelerator shipping; ~10 tok/s on a
  1.5-2.7B; $130-200 as a Pi HAT. Closest to "cool/small enough to sit in a
  device."
- **Front-end ear:** Syntiant NDP120 / GAP9 wake-word at sub-mW — solved, cheap.
- **ESP32 has NO NPU successor announced for 2026-27** — toy stays a thin client.

**On-toy battery LLM is NOT viable in 2026** (8-14W, 8GB, kills battery/BOM).
It's a **2027-2028 event**, driven by these to watch:
- **RK3668 (2026, LPDDR5 100 GB/s)** → **RK3688 (2027, LPDDR6 200 GB/s)** — the
  bandwidth wave; RK3688 is the first cheap SoC that could do 8B interactively.
- **RK1820/1828** co-processor — 3D-stacked DRAM ~1 TB/s → **>100 tok/s on 3B**
  (fixes the bandwidth wall). Price contested ($140 module vs $889 devkit).
- **DeepX DX-M2 (2027, 2nm)** — 20-30 tok/s on 20B at ≤5W; most ambitious.
- **Amlogic A311Y3** (announced 6/2026) — first Amlogic for LLMs.
- **BitNet / 1.58-bit models** — software wildcard: 2B quality at 1.8GB, ~8 tok/s
  on a Pi. Halves the memory wall; best hope for a genuinely on-toy model.

**Phone-as-brain viable now** (A19/M5 ~25 tok/s on 4B; Snapdragon 8 Elite 10-32
on 3B) — blocker is iOS foreground-only GPU; use Apple Foundation Models or an
Android foreground service.

**Verdict:** ship v1 with a bundled ~$40 RK3588 dock (or phone / disciplined
cloud), pluggable brain, then ride the 2027 bandwidth wave to shrink the brain
toward — eventually into — the toy.

## Caveats
- Every non-HHEM number (IFEval/BFCL) is vendor-reported; BFCL-v4 lists none
  of these. HHEM is the only independent grounding signal — trust it, then
  **run our own suite** on the final 2–3. That empirical pass is the decision.
- Mistral tool-calling has documented serving bugs (calls land in `content`);
  use 3.2+ and validate. Nemotron/reasoning models confabulate in CoT — run
  reasoning-OFF for RAG, never trust `<think>` as grounded output.
- Verify exact Ollama tags at pull time; sizes/quants shift.
