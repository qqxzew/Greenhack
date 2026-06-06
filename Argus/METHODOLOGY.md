# Argus — Investor Report Methodology

This document explains, transparently and reproducibly, **how the data in the
investor report is generated**, what every parameter means, and exactly how the
"before vs. after" cost and time figures are computed.

Nothing here is hand-waved. The default run is a **deterministic mock** — no API
key, no spend — so anyone can reproduce the headline numbers byte-for-byte:

```bash
python3.11 make_investor_report.py            # 300 tasks, seed 7 (the headline run)
```

The same workload can be sent to the **real Claude API** with `--live` (needs
`ANTHROPIC_API_KEY`); the accounting is identical, only the responder changes.

---

## 1. What the workload represents

We simulate the stream of LLM calls an **AI-agent platform** makes in normal
operation. Each call is a *task*. Tasks differ in how hard they are, which is the
single most important property because difficulty determines **which model the
work genuinely needs** — and therefore where money is won or lost.

Every task is assigned to one of four **difficulty bands**. Each band declares
the model a competent router *should* pick for it:

| Band     | Complexity range | Required model      | Why                              |
|----------|------------------|---------------------|----------------------------------|
| `simple` | 0.05 – 0.28      | `claude-haiku-4-5`  | cheap, mechanical work           |
| `medium` | 0.38 – 0.60      | `claude-sonnet-4-5` | standard reasoning               |
| `hard`   | 0.60 – 0.69      | `claude-sonnet-4-5` | same model, deeper thinking      |
| `expert` | 0.74 – 0.97      | `claude-opus-4-7`   | frontier-only reasoning          |

The complexity ranges deliberately leave **gaps** between bands (e.g. 0.28→0.38)
so a task is unambiguously inside exactly one band, never straddling a routing
threshold by accident.

The spread matters: it exercises the optimizer in **both directions** — routing
easy work *down* to Haiku (large savings) and genuinely hard work *up* to Opus
(a deliberate quality investment). The report shows both honestly, rather than
only counting the wins.

On top of the band mix, two real agent pathologies are injected at controllable
rates:

- **Cache repeats** — a task whose prompt *exactly* repeats an earlier one. The
  optimizer serves it from cache and the LLM is never called.
- **Stuck loops** — an agent that keeps re-calling with no progress. A sequential
  probability ratio test (SPRT) detects the lack of progress and force-stops it.

---

## 2. The four difficulty presets

`--difficulty` selects the band-weight mix for the **unique** tasks (the ones
that are neither cache repeats nor loops):

| Preset     | simple | medium | hard | expert |
|------------|--------|--------|------|--------|
| `easy`     | 0.60   | 0.28   | 0.08 | 0.04   |
| `balanced` | 0.42   | 0.30   | 0.16 | 0.12   | ← default
| `hard`     | 0.25   | 0.30   | 0.25 | 0.20   |
| `frontier` | 0.15   | 0.25   | 0.28 | 0.32   |

Higher presets push more tasks into `expert`, which is where Opus is genuinely
required — so they raise the *naive* frontier cost and make Argus's right-sizing
worth more. You can also bypass the preset entirely with `--strong-frac` to pin
the exact expert share (e.g. `--strong-frac 0.10` = 10 % Opus-needing tasks),
or pass an explicit `band_weights` dict in code for full control.

Band assignment is **deterministic**: exact integer counts are derived from the
weights, the remainder is handed to the heaviest-weighted bands, then the bag is
shuffled with the seeded RNG. There is no per-task coin flip, so the band mix is
identical on every run with the same seed.

---

## 3. The model tiers, prices, and routing policy

Three models, list prices per **1,000 tokens** (USD):

| Model               | Input    | Output   | Capability rank |
|---------------------|----------|----------|-----------------|
| `claude-haiku-4-5`  | 0.00025  | 0.00125  | 1               |
| `claude-sonnet-4-5` | 0.003    | 0.015    | 2               |
| `claude-opus-4-7`   | 0.015    | 0.075    | 3               |

Argus routes by complexity using these thresholds (first tier whose upper bound
exceeds the task's complexity wins):

```
complexity < 0.33  → claude-haiku-4-5
complexity < 0.70  → claude-sonnet-4-5
otherwise          → claude-opus-4-7
```

These thresholds are aligned with the band ranges in §1, so a correctly-banded
task routes to the model it actually requires.

---

## 4. The quality model (why "cheap everywhere" is not free)

A model **at or above** a task's required capability tier delivers that task's
**ceiling quality** (default `0.93`). A model **below** the required tier loses
`0.13` of quality **per missing tier**, floored at `0.30`:

```
quality(model, required) = ceiling                      if rank(model) >= rank(required)
                         = ceiling − 0.13 × tier_gap     otherwise   (min 0.30)
```

Concretely, on an **expert** task (requires Opus, rank 3):

- Opus → 0.930  (meets it)
- Sonnet → 0.930 − 0.13 × 1 = **0.800**  (one tier short)
- Haiku → 0.930 − 0.13 × 2 = **0.670**  (two tiers short)

This is the crux of the whole comparison: it is **why an all-Sonnet platform
silently under-delivers** on frontier work, and why Argus — by routing those
tasks up to Opus — keeps quality at the ceiling. Savings claimed at *lower*
quality would be dishonest; the report holds quality constant and compares cost.

---

## 5. The three baselines (the heart of the honesty)

A single "before" number can always be gamed. We report Argus against **three**
explicit baselines so the reader can see exactly where each dollar comes from:

1. **flat-Opus** *(naive frontier)* — use the best model for everything. Highest
   quality, wildly overpriced on easy work. This is the naive "just use the
   strongest model" strategy and the headline `−X%` is measured against it.

2. **flat-Sonnet** *(cheap, lower quality)* — one mid-tier model for everything.
   Cheaper than Argus in raw dollars, **but it loses quality on expert tasks**
   (see §4). It is shown precisely so we don't hide that Argus costs more than
   the cheapest possible option — Argus buys back the quality flat-Sonnet drops.

3. **quality-matched** *(right model per task, no Argus)* — the correct model for
   every task, but **without** compression, caching, or loop-stopping. The gap
   between this and Argus is **pure efficiency** — it isolates what the
   optimization layers add on top of good routing alone.

**Argus** is then: right-size every task to the model it needs **and** apply the
efficiency layers (compression, semantic cache, SPRT loop-stopping).

### Cost / time waterfall (exact decomposition)

The drop from flat-Opus to Argus is decomposed into four **non-negative,
additive** components that sum *exactly* to the total:

```
flat-Opus − Argus = ROUTING + COMPRESS + CACHE + STOP
```

- **ROUTING** = flat-Opus − quality-matched (right-size each task to its tier)
- Then each call's remaining `(quality-matched − Argus)` saving is attributed to
  the mechanism that actually fired on that call: **CACHE** (skipped call),
  **STOP** (loop force-stopped), or **COMPRESS** (same model, fewer input tokens).

The same decomposition is applied independently to **cost** (dollars) and to
**time** (seconds), and both are verified to reconcile to the cent / millisecond.

---

## 6. The latency (time) model

Time savings are modeled analytically so a free mock run reproduces the time
chart exactly. Each model has a fixed time-to-first-token plus per-input-token
prefill and per-output-token decode costs (milliseconds):

| Model               | base\_ms | ms / input tok | ms / output tok |
|---------------------|----------|----------------|-----------------|
| `claude-haiku-4-5`  | 180      | 0.020          | 4.0             |
| `claude-sonnet-4-5` | 380      | 0.045          | 11.0            |
| `claude-opus-4-7`   | 700      | 0.070          | 26.0            |
| cache hit           | 6        | —              | —               |

```
latency_ms = base_ms + tokens_in × ms_per_in_tok + tokens_out × ms_per_out_tok
```

Bigger models are slower on every axis, so routing down saves *time* as well as
money, and a cache hit (6 ms) is effectively instantaneous versus a full
generation. A stopped loop saves the wall-clock time of every iteration it
prevented.

---

## 7. What "baseline" means per call

For every task, Argus records the call it *would* have made naively and the call
it *actually* made:

- **baseline** = the full, uncompressed prompt on the **frontier** model — the
  worst case a "use the strong model and never optimize" agent would pay.
- **actual** = the real model Argus chose, on the real (possibly compressed)
  prompt, or **nothing at all** when the call was served from cache or the loop
  was stopped.

Both sides are priced at **list price** (no prefix-cache discount applied to
either side) so the comparison is apples-to-apples and the per-mechanism
decomposition reconciles exactly.

---

## 8. Reproducibility

- A single integer **`--seed`** (default `7`) drives *all* randomness: band
  assignment, complexity within a band, domain choice, and the numeric details
  inside each prompt. The cache-repeat / loop interleave uses `seed + 1`.
- Same seed + same flags ⇒ **byte-identical** task list and identical report.
- The default run makes **no API calls** and costs **nothing**.
- Token counts use a transparent estimator (`len(text) // 4`); with `--live`
  the real `usage` from the API is used instead.

Every run also writes machine-readable artefacts next to the HTML:

```
reports/investor_<timestamp>/
  investor_report.html          # the self-contained, shareable page
  summary.json                  # all computed stats
  data/dataset_manifest.json    # how the workload was built (auditable)
  data/worked_examples.json     # the five concrete per-task examples shown on the page
  data/calls.jsonl              # one line per call: baseline vs actual
```

Each run **deletes the previous `reports/investor_*` directories first**, so the
folder always holds exactly one fresh report (pass `--keep-old` to retain them).
The report's "Worked examples" section is drawn from real calls in this run —
one per optimization mechanism (down-route, up-route, compression, cache, loop-stop)
— each showing the actual prompt, the exact method that fired, and the concrete
before→after cost / time / quality. The `call #` on each card is the line index
in `data/calls.jsonl`, so every example is independently verifiable.

---

## 9. Full configuration knobs

Every knob is exposed on the command line (`python3.11 make_investor_report.py --help`):

| Flag                 | Default    | Meaning                                                        |
|----------------------|------------|----------------------------------------------------------------|
| `--n`                | `300`      | number of tasks generated (scale)                              |
| `--seed`             | `7`        | RNG seed — full reproducibility                                |
| `--difficulty`       | `balanced` | band-weight preset: `easy` / `balanced` / `hard` / `frontier`  |
| `--strong-frac`      | *(preset)* | override the expert (Opus-needing) share directly, e.g. `0.10` |
| `--reasoning-depth`  | `1.0`      | scales reasoning output tokens on `hard` + `expert` tasks      |
| `--cache-rate`       | `0.12`     | fraction of tasks that are exact cache repeats                 |
| `--loop-rate`        | `0.04`     | fraction of tasks that are stuck loops (SPRT-stopped)          |
| `--compressible`     | `0.65`     | fraction of verbose tasks the compressor is allowed to shrink  |
| `--live`             | *(off)*    | use the real Claude API instead of the deterministic mock      |
| `--keep-old`         | *(off)*    | keep previous `reports/investor_*` dirs (default deletes them)  |

`--reasoning-depth` multiplies the per-band reasoning-token budget (`reason_out`:
0 for simple, 70 medium, 230 hard, 430 expert), so `1.6` makes hard/expert tasks
generate ~60 % more reasoning tokens — useful to show how cost scales with deeper
thinking on exactly the tasks that need it.

### Example invocations

```bash
# headline 300-task run (free, reproducible)
python3.11 make_investor_report.py

# bigger workload to demonstrate scalability
python3.11 make_investor_report.py --n 2000

# a frontier-heavy mix with deeper reasoning
python3.11 make_investor_report.py --difficulty frontier --reasoning-depth 1.6

# dial individual knobs
python3.11 make_investor_report.py --n 500 --strong-frac 0.08 \
    --cache-rate 0.18 --loop-rate 0.05 --compressible 0.7 --seed 42

# real Claude API calls (needs ANTHROPIC_API_KEY)
python3.11 make_investor_report.py --n 60 --live
```

---

## 10. Honest about modeled vs. measured

In the default **mock** run, the following are **modeled analytically** (not
measured against a live API), specifically so the report is free and
bit-for-bit reproducible:

- **Quality** — derived from the capability-gap rule in §4.
- **Latency** — derived from the per-model profile in §6.
- **Token counts** — estimated as `len(text) // 4`.

What is **real even in mock mode**: the *mechanisms* themselves. The semantic
cache, the MinHash dedup, the SPRT loop-stopper, and the context compressor are
the **production code paths** — the report drives the same components the live
system uses, not a stand-in. With `--live`, quality/latency/tokens come straight
from the Claude API while the accounting and decomposition are unchanged.

The claim we make is therefore precise: *given these transparent, stated
assumptions about model price, latency, and quality, this is exactly how much an
agent platform's bill and wall-clock time shrink when each task is right-sized
and the redundant work is removed.* Change the assumptions in this document and
the numbers move accordingly — that is the point of publishing them.

---

## 11. Two tools, two baselines (offline report vs. live dashboard)

There are **two** separate surfaces, and they use **different baselines on
purpose** — do not expect their headline percentages to match:

| Surface | Baseline ("without Argus") | Why |
|---------|----------------------------|-----|
| **Offline investor report** (`make_investor_report.py`, §5) | **flat-Opus** (best model for everything) | demonstrates the maximum waste of the naive "just use the strongest model" strategy across a designed 3-tier workload |
| **Live dashboard** (`Argus/api/routes.py` → Live Stream, Impact Report, Analytics) | **per-call**: standard model (Sonnet, `LIVE_BASELINE_MODEL`), except a call routed **up** to the frontier model (Opus) is compared to **itself** | the live router routes haiku ↔ sonnet ↔ opus. A down-route to haiku is measured against Sonnet (real saving). An up-route to Opus is a deliberate **quality investment**, so its baseline is Opus → $0 cost-saved, never "Argus is more expensive" |

The live dashboard's savings come from two honest sources only: **down-routing**
(haiku instead of Sonnet) and **skipped calls** (cache / dedup). A call genuinely
**routed up to Opus** shows **$0 cost-saved** and is labelled *“routed up — quality
investment”* (method `route_up`) — it is the frontier baseline, not a cost win.
This per-call baseline (`_baseline_for()` in `routes.py`) deliberately avoids both
failure modes: it never inflates numbers to a flat-Opus strawman, and it never
makes an up-routed call look like Argus overspent.

The global `tracking.BASELINE_MODEL` (Sonnet) is **left untouched** — it belongs
to the offline report path; the live dashboard uses its own `LIVE_BASELINE_MODEL`.

Every live view (stream rows, the Impact Report, the org-level eco counters)
reads from **one cumulative aggregate** (`_agg` / `_savings_snapshot()` in
`routes.py`), so within the live dashboard all "saved" figures — cost, time,
tokens, per-agent, per-method — reconcile to the same totals. (Earlier the org
counter used an `avoided_calls × avg_tokens` estimate while the report summed
per-call `baseline − actual`; those two methods disagreed and have been unified.)
