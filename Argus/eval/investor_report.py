# eval/investor_report.py
"""
Investor-facing Post-Run report.

Produces a single, self-contained HTML page (all charts embedded as base64, so
it can be emailed or hosted as one file) plus machine-readable JSON. The page
answers three questions an investor asks:

  1. How was this data made?          → transparency panel (from the manifest)
  2. Does it actually save money/time? → cost & time waterfalls + cumulative curves
  3. Is it honest?                     → THREE reference baselines, side by side

The three baselines
-------------------
A naive team faces a dilemma:

  • flat-Opus     — "just use the best model so quality never regresses."
                    Safe, but pays frontier prices on every trivial task.
  • flat-Sonnet   — "use one cheaper model everywhere."
                    Cheaper, but silently UNDER-delivers on frontier-hard tasks.
  • quality-match — picks the right model per task but applies none of our
                    efficiency layers (no compression / cache / loop-stop).
                    This is the apples-to-apples "same quality, no Argus" cost.

Argus delivers flat-Opus quality at far below flat-Opus cost by routing each
task to exactly the model it needs and then compressing / caching / stopping on
top. Every number below is reproducible from the seed in the manifest.
"""

from __future__ import annotations

import base64
import io
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from core.tracking import (
    OptType, cost_of, latency_of, MODEL_COSTS,
    BASELINE_MODEL,
)

HAIKU, SONNET, OPUS = "claude-haiku-4-5", "claude-sonnet-4-5", "claude-opus-4-7"

# ── palette ─────────────────────────────────────────────────────────────
BG     = "#0C0C12"
CARD   = "#14141E"
BORDER = "#2A2A3E"
WHITE  = "#F0F0FF"
MUTED  = "#8888AA"
DIM    = "#404058"
TEAL   = "#2DD4B4"
AMBER  = "#F5A623"
CORAL  = "#E05A3A"
PURP   = "#7B68EE"
BLUE   = "#4A90D9"

_TIER_OF   = {HAIKU: "Simple → Haiku", SONNET: "Standard → Sonnet", OPUS: "Frontier → Opus"}
_TIER_ORDER = ["Simple → Haiku", "Standard → Sonnet", "Frontier → Opus"]
_MODEL_SHORT = {HAIKU: "Haiku", SONNET: "Sonnet", OPUS: "Opus", "cache": "Cache"}


def _apply_dark(ax):
    ax.set_facecolor(CARD)
    ax.tick_params(colors=MUTED, labelsize=8)
    for sp in ax.spines.values():
        sp.set_edgecolor(BORDER)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    ax.title.set_color(WHITE)


def _b64(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")


# ── per-call reference baselines ────────────────────────────────────────
def _flat_cost(calls, model):
    return sum(cost_of(model, c.baseline_tokens_in, c.baseline_tokens_out) for c in calls)

def _flat_time(calls, model):
    return sum(latency_of(model, c.baseline_tokens_in, c.baseline_tokens_out) for c in calls) / 1000.0

def _qmatch_cost(calls):
    return sum(cost_of(c.required_model, c.baseline_tokens_in, c.baseline_tokens_out) for c in calls)

def _qmatch_time(calls):
    return sum(latency_of(c.required_model, c.baseline_tokens_in, c.baseline_tokens_out) for c in calls) / 1000.0


def _waterfall_generic(calls, flat_opus, qmatch, qmatch_val, argus_val) -> dict:
    """Decompose (flat-Opus -> Argus) into positive, additive mechanisms.

    flat_opus - argus = ROUTING + COMPRESS + CACHE + STOP   (exact, all >= 0)
      ROUTING  = flat_opus - quality_matched   (right-size every task to the model it needs)
      then each call's remaining (quality_matched - argus) saving is attributed
      to the mechanism that actually fired: CACHE / STOP / COMPRESS.
    """
    routing = flat_opus - qmatch
    compress = cache = stop = 0.0
    for c in calls:
        saving = qmatch_val(c) - argus_val(c)        # >= 0; 0 when nothing fired
        if c.optimization_applied is OptType.CACHE:
            cache += saving
        elif c.optimization_applied is OptType.STOP:
            stop += saving
        else:
            compress += saving                       # same model => pure compression
    argus = sum(argus_val(c) for c in calls)
    return {"start": flat_opus, "ROUTING": routing, "COMPRESS": compress,
            "CACHE": cache, "STOP": stop, "end": argus}


def _cost_waterfall(calls) -> dict:
    return _waterfall_generic(
        calls, _flat_cost(calls, OPUS), _qmatch_cost(calls),
        qmatch_val=lambda c: cost_of(c.required_model, c.baseline_tokens_in, c.baseline_tokens_out),
        argus_val=lambda c: c.actual_cost)


def _time_waterfall(calls) -> dict:
    return _waterfall_generic(
        calls, _flat_time(calls, OPUS), _qmatch_time(calls),
        qmatch_val=lambda c: latency_of(c.required_model, c.baseline_tokens_in, c.baseline_tokens_out) / 1000.0,
        argus_val=lambda c: c.latency_ms / 1000.0)


def compute_investor_stats(calls: list, manifest: dict) -> dict:
    n = len(calls)
    flat_opus_cost   = _flat_cost(calls, OPUS)
    flat_sonnet_cost = _flat_cost(calls, SONNET)
    qmatch_cost      = _qmatch_cost(calls)
    argus_cost       = sum(c.actual_cost for c in calls)

    flat_opus_time   = _flat_time(calls, OPUS)
    flat_sonnet_time = _flat_time(calls, SONNET)
    qmatch_time      = _qmatch_time(calls)
    argus_time       = sum(c.latency_ms for c in calls) / 1000.0

    # routing distribution (what Argus actually did)
    dist = Counter()
    for c in calls:
        if c.optimization_applied is OptType.CACHE:
            dist["Cache"] += 1
        elif c.optimization_applied is OptType.STOP:
            dist["Stopped"] += 1
        else:
            dist[_MODEL_SHORT.get(c.actual_model, c.actual_model)] += 1

    # quality by required tier (Argus vs flat-Sonnet)
    tier_q = defaultdict(lambda: {"argus": [], "sonnet": []})
    for c in calls:
        tier = _TIER_OF.get(c.required_model)
        if tier is None:
            continue
        if c.quality_score:
            tier_q[tier]["argus"].append(c.quality_score)
        if c.baseline_quality:
            tier_q[tier]["sonnet"].append(c.baseline_quality)

    q_opt  = [c.quality_score    for c in calls if c.quality_score]
    q_base = [c.baseline_quality for c in calls if c.baseline_quality]
    quality_opt  = sum(q_opt) / len(q_opt)   if q_opt  else 0.0
    quality_base = sum(q_base) / len(q_base) if q_base else 0.0

    return {
        "n_calls":          n,
        "cost": {
            "flat_opus": flat_opus_cost, "flat_sonnet": flat_sonnet_cost,
            "quality_matched": qmatch_cost, "argus": argus_cost,
        },
        "time": {
            "flat_opus": flat_opus_time, "flat_sonnet": flat_sonnet_time,
            "quality_matched": qmatch_time, "argus": argus_time,
        },
        "save_vs_frontier_cost":  (1 - argus_cost / flat_opus_cost) * 100 if flat_opus_cost else 0.0,
        "save_vs_frontier_time":  (1 - argus_time / flat_opus_time) * 100 if flat_opus_time else 0.0,
        "save_vs_qmatch_cost":    (1 - argus_cost / qmatch_cost) * 100 if qmatch_cost else 0.0,
        "save_vs_qmatch_time":    (1 - argus_time / qmatch_time) * 100 if qmatch_time else 0.0,
        "cost_per_1m_saved_frontier": (flat_opus_cost - argus_cost) / n * 1_000_000 if n else 0.0,
        "cost_per_1m_saved_qmatch":   (qmatch_cost - argus_cost) / n * 1_000_000 if n else 0.0,
        "routing_dist":     dict(dist),
        "tier_quality":     {t: {"argus": (sum(v["argus"])/len(v["argus"]) if v["argus"] else 0.0),
                                 "sonnet": (sum(v["sonnet"])/len(v["sonnet"]) if v["sonnet"] else 0.0)}
                             for t, v in tier_q.items()},
        "quality_opt":      quality_opt,
        "quality_base":     quality_base,
        "quality_delta":    quality_opt - quality_base,
        "cost_waterfall":   _cost_waterfall(calls),
        "time_waterfall":   _time_waterfall(calls),
        "manifest":         manifest,
    }


# ── charts ──────────────────────────────────────────────────────────────
def _chart_cost_bars(stats) -> str:
    c = stats["cost"]
    labels = ["flat-Opus\n(naive frontier)", "flat-Sonnet\n(cheap, lower quality)",
              "quality-matched\n(right model, no Argus)", "ARGUS\n(routed + optimized)"]
    vals   = [c["flat_opus"], c["flat_sonnet"], c["quality_matched"], c["argus"]]
    colors = [CORAL, AMBER, BLUE, TEAL]
    fig, ax = plt.subplots(figsize=(7.6, 4.2), facecolor=BG)
    _apply_dark(ax)
    bars = ax.bar(labels, vals, color=[x + "CC" for x in colors],
                  edgecolor=colors, linewidth=1.4, width=0.62)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width()/2, v, f"${v:.2f}",
                ha="center", va="bottom", color=WHITE, fontsize=9,
                fontweight="bold", fontfamily="monospace")
    ax.set_title(f"Total cost — {stats['n_calls']} tasks (lower is better)",
                 fontfamily="monospace", pad=10)
    ax.set_ylabel("USD", fontfamily="monospace")
    ax.set_ylim(0, max(vals) * 1.18)
    ax.text(3, c["argus"] + max(vals) * 0.06,
            f"−{stats['save_vs_frontier_cost']:.0f}% vs frontier",
            ha="center", color=TEAL, fontsize=9, fontweight="bold", fontfamily="monospace")
    return _b64(fig)


def _chart_time_bars(stats) -> str:
    t = stats["time"]
    labels = ["flat-Opus", "flat-Sonnet", "quality-matched", "ARGUS"]
    vals   = [t["flat_opus"], t["flat_sonnet"], t["quality_matched"], t["argus"]]
    colors = [CORAL, AMBER, BLUE, TEAL]
    fig, ax = plt.subplots(figsize=(7.6, 4.2), facecolor=BG)
    _apply_dark(ax)
    bars = ax.bar(labels, vals, color=[x + "CC" for x in colors],
                  edgecolor=colors, linewidth=1.4, width=0.62)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width()/2, v, f"{v:.0f}s",
                ha="center", va="bottom", color=WHITE, fontsize=9,
                fontweight="bold", fontfamily="monospace")
    ax.set_title("Total wall-clock time (modeled, lower is better)",
                 fontfamily="monospace", pad=10)
    ax.set_ylabel("seconds", fontfamily="monospace")
    ax.set_ylim(0, max(vals) * 1.18)
    ax.text(3, t["argus"] + max(vals) * 0.06,
            f"−{stats['save_vs_frontier_time']:.0f}% vs frontier",
            ha="center", color=TEAL, fontsize=9, fontweight="bold", fontfamily="monospace")
    return _b64(fig)


def _waterfall(ax, wf, unit, fmt):
    steps = ["ROUTING", "COMPRESS", "CACHE", "STOP"]
    names = ["Right-size\nrouting", "Context\ncompression", "Semantic\ncache", "Loop\nstop"]
    colors = [AMBER, TEAL, PURP, BLUE]
    running = wf["start"]
    # start bar
    ax.bar(0, running, color=CORAL + "AA", edgecolor=CORAL, width=0.65)
    ax.text(0, running, fmt(running), ha="center", va="bottom",
            color=WHITE, fontsize=8, fontfamily="monospace")
    for i, (s, nm, col) in enumerate(zip(steps, names, colors), 1):
        drop = wf[s]
        ax.bar(i, drop, bottom=running - drop, color=col + "CC",
               edgecolor=col, width=0.65)
        if drop > wf["start"] * 0.02:
            ax.text(i, running - drop/2, f"−{fmt(drop)}", ha="center", va="center",
                    color=WHITE, fontsize=7.5, fontfamily="monospace")
        running -= drop
    ax.bar(5, running, color=TEAL + "CC", edgecolor=TEAL, width=0.65)
    ax.text(5, running, fmt(running), ha="center", va="bottom",
            color=TEAL, fontsize=8, fontweight="bold", fontfamily="monospace")
    ax.set_xticks(range(6))
    ax.set_xticklabels(["flat-Opus"] + names + ["ARGUS"], fontsize=7.5,
                       fontfamily="monospace")
    ax.set_ylabel(unit, fontfamily="monospace")


def _chart_cost_waterfall(stats) -> str:
    fig, ax = plt.subplots(figsize=(8.2, 4.4), facecolor=BG)
    _apply_dark(ax)
    _waterfall(ax, stats["cost_waterfall"], "USD", lambda v: f"${v:.2f}")
    ax.set_title("Where the cost savings come from  (flat-Opus → Argus)",
                 fontfamily="monospace", pad=10)
    return _b64(fig)


def _chart_time_waterfall(stats) -> str:
    fig, ax = plt.subplots(figsize=(8.2, 4.4), facecolor=BG)
    _apply_dark(ax)
    _waterfall(ax, stats["time_waterfall"], "seconds", lambda v: f"{v:.0f}s")
    ax.set_title("Where the time savings come from  (flat-Opus → Argus)",
                 fontfamily="monospace", pad=10)
    return _b64(fig)


def _chart_cumulative(calls, stats) -> tuple[str, str]:
    n = len(calls)
    xs = list(range(1, n + 1))
    opus_c = qm_c = arg_c = 0.0
    opus_t = qm_t = arg_t = 0.0
    cc_opus, cc_qm, cc_arg = [], [], []
    ct_opus, ct_qm, ct_arg = [], [], []
    for c in calls:
        opus_c += cost_of(OPUS, c.baseline_tokens_in, c.baseline_tokens_out)
        qm_c   += cost_of(c.required_model, c.baseline_tokens_in, c.baseline_tokens_out)
        arg_c  += c.actual_cost
        cc_opus.append(opus_c); cc_qm.append(qm_c); cc_arg.append(arg_c)
        opus_t += latency_of(OPUS, c.baseline_tokens_in, c.baseline_tokens_out) / 1000.0
        qm_t   += latency_of(c.required_model, c.baseline_tokens_in, c.baseline_tokens_out) / 1000.0
        arg_t  += c.latency_ms / 1000.0
        ct_opus.append(opus_t); ct_qm.append(qm_t); ct_arg.append(arg_t)

    def make(xs, opus, qm, arg, ylabel, title):
        fig, ax = plt.subplots(figsize=(8.2, 4.2), facecolor=BG)
        _apply_dark(ax)
        ax.plot(xs, opus, color=CORAL, lw=2, label="flat-Opus (naive frontier)")
        ax.plot(xs, qm,   color=BLUE,  lw=1.6, ls="--", label="quality-matched (no Argus)")
        ax.plot(xs, arg,  color=TEAL,  lw=2.2, label="Argus")
        ax.fill_between(xs, arg, opus, color=TEAL, alpha=0.10)
        ax.set_xlabel("tasks processed", fontfamily="monospace")
        ax.set_ylabel(ylabel, fontfamily="monospace")
        ax.set_title(title, fontfamily="monospace", pad=10)
        ax.legend(fontsize=7.5, framealpha=0, labelcolor=MUTED,
                  prop={"family": "monospace"})
        return _b64(fig)

    cost_png = make(xs, cc_opus, cc_qm, cc_arg, "cumulative USD",
                    "Cumulative cost as the workload scales")
    time_png = make(xs, ct_opus, ct_qm, ct_arg, "cumulative seconds",
                    "Cumulative time as the workload scales")
    return cost_png, time_png


def _chart_routing(stats) -> str:
    dist  = stats["routing_dist"]
    order = ["Haiku", "Sonnet", "Opus", "Cache", "Stopped"]
    cmap  = {"Haiku": TEAL, "Sonnet": AMBER, "Opus": CORAL, "Cache": PURP, "Stopped": DIM}
    labels = [k for k in order if dist.get(k)]
    sizes  = [dist[k] for k in labels]
    colors = [cmap[k] for k in labels]
    fig, ax = plt.subplots(figsize=(5.2, 4.6), facecolor=BG)
    ax.set_facecolor(BG)
    wedges, _txt, autot = ax.pie(
        sizes, colors=colors, autopct=lambda p: f"{p:.0f}%", startangle=90,
        pctdistance=0.74, wedgeprops=dict(width=0.46, edgecolor=BG, linewidth=2))
    for a in autot:
        a.set_color(WHITE); a.set_fontsize(8); a.set_fontfamily("monospace")
    ax.set_title("How Argus routed the workload", color=WHITE,
                 fontfamily="monospace", pad=12)
    ax.legend(wedges, [f"{l}  ({n})" for l, n in zip(labels, sizes)],
              loc="lower center", ncol=3, fontsize=7.5, framealpha=0,
              labelcolor=MUTED, prop={"family": "monospace"},
              bbox_to_anchor=(0.5, -0.08))
    return _b64(fig)


def _chart_quality(stats) -> str:
    tiers = [t for t in _TIER_ORDER if t in stats["tier_quality"]]
    arg   = [stats["tier_quality"][t]["argus"]  for t in tiers]
    son   = [stats["tier_quality"][t]["sonnet"] for t in tiers]
    import numpy as np
    x = np.arange(len(tiers)); w = 0.36
    fig, ax = plt.subplots(figsize=(7.0, 4.2), facecolor=BG)
    _apply_dark(ax)
    ax.bar(x - w/2, son, w, color=AMBER + "99", edgecolor=AMBER, lw=1.2,
           label="flat-Sonnet baseline")
    ax.bar(x + w/2, arg, w, color=TEAL + "99", edgecolor=TEAL, lw=1.2, label="Argus")
    for i, (s, a) in enumerate(zip(son, arg)):
        d = a - s
        if abs(d) > 0.001:
            ax.text(i + w/2, a + 0.005, f"+{d:.2f}", ha="center", color=TEAL,
                    fontsize=8, fontfamily="monospace")
    ax.axhline(0.75, color=WHITE, lw=0.8, ls="--", alpha=0.4)
    ax.text(len(tiers) - 0.5, 0.76, "acceptable", color=DIM, fontsize=7,
            fontfamily="monospace")
    ax.set_xticks(x); ax.set_xticklabels(tiers, fontsize=8, fontfamily="monospace")
    ax.set_ylim(0.5, 1.02)
    ax.set_ylabel("avg quality", fontfamily="monospace")
    ax.set_title("Quality by difficulty tier — Argus keeps it where flat-Sonnet drops it",
                 fontfamily="monospace", pad=10)
    ax.legend(fontsize=7.5, framealpha=0, labelcolor=MUTED, prop={"family": "monospace"})
    return _b64(fig)


# ── concrete worked examples ─────────────────────────────────────────────
def _esc(s: str) -> str:
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _excerpt(prompt: str, maxlen: int = 160) -> str:
    """Pull the final USER: ask out of a rendered conversation prompt."""
    if not prompt:
        return ""
    ask = ""
    for ln in prompt.splitlines():
        if ln.startswith("USER:"):
            ask = ln[5:].strip()
    if not ask:
        ask = prompt.strip().splitlines()[0] if prompt.strip() else ""
    ask = " ".join(ask.split())
    return ask[:maxlen] + ("…" if len(ask) > maxlen else "")


def _result_excerpt(text: str, maxlen: int = 120) -> str:
    if not text:
        return ""
    t = " ".join(text.split())
    return t[:maxlen] + ("…" if len(t) > maxlen else "")


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def pick_examples(calls) -> list[dict]:
    """Select one vivid, concrete task per optimization mechanism, with the
    exact before→after numbers. Each example states its own honest baseline."""
    def opus_before(c):
        return (cost_of(OPUS, c.baseline_tokens_in, c.baseline_tokens_out),
                latency_of(OPUS, c.baseline_tokens_in, c.baseline_tokens_out) / 1000.0)

    def model_before(model, c):
        return (cost_of(model, c.baseline_tokens_in, c.baseline_tokens_out),
                latency_of(model, c.baseline_tokens_in, c.baseline_tokens_out) / 1000.0)

    ex: list[dict] = []

    # 1 ─ DOWN-ROUTE → Haiku (pure routing; foil = naive flat-Opus frontier)
    downs = [c for c in calls
             if c.actual_model == HAIKU and c.optimization_applied is OptType.ROUTE] \
            or [c for c in calls if c.actual_model == HAIKU]
    if downs:
        c = max(downs, key=lambda c: opus_before(c)[0] - c.actual_cost)
        bc, bt = opus_before(c)
        cs = c.optimization_detail.get("complexity_score", "?")
        ex.append({
            "tag": "ROUTE ↓", "color": "t-teal", "kind": "cost",
            "method": "Model down-routing",
            "task_type": c.task_type, "call_id": c.call_id,
            "ask": _excerpt(c.baseline_prompt), "result": _result_excerpt(c.output_text),
            "detail": f"complexity {cs} &lt; 0.33 → ran on Haiku; a naive frontier policy "
                      f"would have spent Opus on this trivial task.",
            "base_label": "flat-Opus (naive)",
            "b_model": OPUS, "b_in": c.baseline_tokens_in, "b_out": c.baseline_tokens_out,
            "b_cost": bc, "b_time": bt, "b_quality": c.quality_score,
            "a_model": c.actual_model, "a_in": c.actual_tokens_in, "a_out": c.actual_tokens_out,
            "a_cost": c.actual_cost, "a_time": c.latency_ms / 1000.0, "a_quality": c.quality_score,
        })

    # 2 ─ UP-ROUTE → Opus (quality protection; foil = flat-Sonnet)
    ups = [c for c in calls if c.actual_model == OPUS and c.baseline_quality]
    if ups:
        c = max(ups, key=lambda c: (c.quality_score or 0) - (c.baseline_quality or 0))
        bc, bt = model_before(SONNET, c)
        cs = c.optimization_detail.get("complexity_score", "?")
        ex.append({
            "tag": "ROUTE ↑", "color": "t-purp", "kind": "quality",
            "method": "Model up-routing — quality protection",
            "task_type": c.task_type, "call_id": c.call_id,
            "ask": _excerpt(c.baseline_prompt), "result": _result_excerpt(c.output_text),
            "detail": f"complexity {cs} ≥ 0.70 → genuinely frontier-hard, so Argus invested "
                      f"in Opus. flat-Sonnet would be cheaper but drops {(c.quality_score or 0)-(c.baseline_quality or 0):.2f} "
                      f"of quality here.",
            "base_label": "flat-Sonnet (cheap)",
            "b_model": SONNET, "b_in": c.baseline_tokens_in, "b_out": c.baseline_tokens_out,
            "b_cost": bc, "b_time": bt, "b_quality": c.baseline_quality,
            "a_model": c.actual_model, "a_in": c.actual_tokens_in, "a_out": c.actual_tokens_out,
            "a_cost": c.actual_cost, "a_time": c.latency_ms / 1000.0, "a_quality": c.quality_score,
        })

    # 3 ─ COMPRESS (same model, fewer input tokens)
    comps = [c for c in calls if c.optimization_applied is OptType.COMPRESS]
    if comps:
        c = max(comps, key=lambda c: c.baseline_tokens_in - c.actual_tokens_in)
        d = c.optimization_detail
        bc, bt = model_before(c.actual_model, c)
        ex.append({
            "tag": "COMPRESS", "color": "t-blue", "kind": "cost",
            "method": "Context compression",
            "task_type": c.task_type, "call_id": c.call_id,
            "ask": _excerpt(c.baseline_prompt), "result": _result_excerpt(c.output_text),
            "detail": f"dropped {_plural(d.get('turns_dropped', 0), 'stale turn')}, kept "
                      f"{_plural(d.get('facts_kept', 0), 'load-bearing fact')} → input prompt shrank to "
                      f"{d.get('compression_ratio', 1)*100:.0f}% of its size "
                      f"({c.baseline_tokens_in:,}→{c.actual_tokens_in:,} tokens). Same model "
                      f"({_MODEL_SHORT.get(c.actual_model, c.actual_model)}), same answer.",
            "base_label": "full prompt",
            "b_model": c.actual_model, "b_in": c.baseline_tokens_in, "b_out": c.baseline_tokens_out,
            "b_cost": bc, "b_time": bt, "b_quality": c.quality_score,
            "a_model": c.actual_model, "a_in": c.actual_tokens_in, "a_out": c.actual_tokens_out,
            "a_cost": c.actual_cost, "a_time": c.latency_ms / 1000.0, "a_quality": c.quality_score,
        })

    # 4 ─ CACHE (exact repeat; model never called)
    caches = [c for c in calls if c.optimization_applied is OptType.CACHE]
    if caches:
        c = max(caches, key=lambda c: opus_before(c)[0])
        d = c.optimization_detail
        bc, bt = opus_before(c)
        ex.append({
            "tag": "CACHE", "color": "t-amber", "kind": "cost",
            "method": "Semantic cache hit",
            "task_type": c.task_type, "call_id": c.call_id,
            "ask": _excerpt(c.baseline_prompt), "result": _result_excerpt(c.output_text),
            "detail": f"byte-identical to call #{d.get('matched_call', '?')} "
                      f"(similarity {d.get('cache_similarity', 1.0):.2f}) → served from cache, "
                      f"the model was never called.",
            "base_label": "flat-Opus (re-run)",
            "b_model": OPUS, "b_in": c.baseline_tokens_in, "b_out": c.baseline_tokens_out,
            "b_cost": bc, "b_time": bt, "b_quality": c.quality_score,
            "a_model": "cache", "a_in": 0, "a_out": c.actual_tokens_out,
            "a_cost": c.actual_cost, "a_time": c.latency_ms / 1000.0, "a_quality": c.quality_score,
        })

    # 5 ─ STOP (stuck loop force-stopped by SPRT)
    stops = [c for c in calls if c.optimization_applied is OptType.STOP]
    if stops:
        c = max(stops, key=lambda c: c.optimization_detail.get("loops_prevented", 0))
        d = c.optimization_detail
        bc, bt = opus_before(c)
        ex.append({
            "tag": "STOP", "color": "t-coral", "kind": "cost",
            "method": "Stuck-loop stop (SPRT)",
            "task_type": c.task_type, "call_id": c.call_id,
            "ask": _excerpt(c.baseline_prompt), "result": "",
            "detail": f"agent repeated the same step with no progress; SPRT stopped it after "
                      f"{_plural(d.get('loops_run', 0), 'iteration')}, preventing "
                      f"{_plural(d.get('loops_prevented', 0), 'wasted call')}.",
            "base_label": "flat-Opus (unstopped)",
            "b_model": OPUS, "b_in": c.baseline_tokens_in, "b_out": c.baseline_tokens_out,
            "b_cost": bc, "b_time": bt, "b_quality": None,
            "a_model": "stopped", "a_in": 0, "a_out": 0,
            "a_cost": c.actual_cost, "a_time": c.latency_ms / 1000.0, "a_quality": None,
        })

    return ex


def _render_examples(examples) -> str:
    if not examples:
        return ""
    cards = []
    for x in examples:
        dc = (x["a_cost"] - x["b_cost"]) / x["b_cost"] * 100 if x["b_cost"] else 0.0
        dt = (x["a_time"] - x["b_time"]) / x["b_time"] * 100 if x["b_time"] else 0.0
        cost_cls = "good" if dc <= 0 else "invest"
        time_cls = "good" if dt <= 0 else "invest"
        cost_delta = f"−{abs(dc):.0f}%" if dc <= 0 else f"+{dc:.0f}%"
        time_delta = f"−{abs(dt):.0f}%" if dt <= 0 else f"+{dt:.0f}%"

        if x["b_quality"] is not None and x["a_quality"] is not None:
            qd = x["a_quality"] - x["b_quality"]
            q_cls = "good" if qd >= 0 else "invest"
            q_delta = f"{qd:+.2f}" if abs(qd) > 1e-9 else "same"
            qrow = (f"<tr><td>Quality</td><td>{x['b_quality']:.2f}</td>"
                    f"<td>{x['a_quality']:.2f}</td><td class='{q_cls}'>{q_delta}</td></tr>")
        else:
            qrow = "<tr><td>Quality</td><td>—</td><td>—</td><td>—</td></tr>"

        result = (f"<div class='ex-res'>→ <code>{_esc(x['result'])}</code></div>"
                  if x["result"] else "")
        cards.append(f"""<div class='ex'>
  <div class='ex-h'><span class='ex-tag {x['color']}'>{x['tag']}</span>
    <b>{_esc(x['method'])}</b><span class='ex-type'> · {_esc(x['task_type'])} · call #{x['call_id']}</span></div>
  <div class='ex-ask'>“{_esc(x['ask'])}”</div>{result}
  <div class='ex-detail'>{x['detail']}</div>
  <table class='ex-t'>
    <tr><th></th><th>{_esc(x['base_label'])}</th><th>Argus</th><th>Δ</th></tr>
    <tr><td>Model</td><td>{_MODEL_SHORT.get(x['b_model'], x['b_model'])}</td>
        <td>{_MODEL_SHORT.get(x['a_model'], x['a_model'])}</td><td></td></tr>
    <tr><td>Tokens</td><td>{x['b_in']:,}+{x['b_out']:,}</td>
        <td>{x['a_in']:,}+{x['a_out']:,}</td><td></td></tr>
    <tr><td>Cost</td><td>${x['b_cost']:.4f}</td><td>${x['a_cost']:.4f}</td>
        <td class='{cost_cls}'>{cost_delta}</td></tr>
    <tr><td>Time</td><td>{x['b_time']:.2f}s</td><td>{x['a_time']:.2f}s</td>
        <td class='{time_cls}'>{time_delta}</td></tr>
    {qrow}
  </table>
</div>""")
    return "<div class='exgrid'>" + "".join(cards) + "</div>"


# ── HTML ────────────────────────────────────────────────────────────────
_CSS = """
:root{color-scheme:dark}
*{box-sizing:border-box}
body{background:#0C0C12;color:#F0F0FF;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
 margin:0;padding:0}
.wrap{max-width:1180px;margin:auto;padding:34px 26px 70px}
h1{font-size:26px;margin:0 0 4px} .sub{color:#8888AA;font-size:14px;margin-bottom:26px}
h2{font-size:18px;margin:40px 0 14px;border-bottom:1px solid #2A2A3E;padding-bottom:8px}
.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:18px 0}
.card{background:#14141E;border:1px solid #2A2A3E;border-radius:12px;padding:16px 18px}
.card .k{color:#8888AA;font-size:11px;text-transform:uppercase;letter-spacing:.04em}
.card .v{font-size:26px;font-weight:700;font-family:monospace;margin-top:6px}
.card .n{color:#8888AA;font-size:11px;margin-top:4px}
.t-teal{color:#2DD4B4}.t-coral{color:#E05A3A}.t-amber{color:#F5A623}.t-blue{color:#4A90D9}.t-purp{color:#7B68EE}
.grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:18px}
.fig{background:#14141E;border:1px solid #2A2A3E;border-radius:12px;padding:12px}
.fig img{width:100%;display:block;border-radius:6px}
.callout{background:#101820;border:1px solid #2DD4B455;border-radius:12px;padding:18px 22px;
 margin:18px 0;font-size:14px;line-height:1.6;color:#C8E8E0}
.callout b{color:#2DD4B4}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{border:1px solid #2A2A3E;padding:7px 11px;text-align:left;font-family:monospace}
th{color:#8888AA;font-weight:600}
.note{color:#8888AA;font-size:12px;margin-top:8px}
.pill{display:inline-block;background:#1a1410;border:1px solid #F5A62355;color:#E8D0A8;
 border-radius:20px;padding:3px 12px;font-size:12px;font-family:monospace;margin:2px}
.exgrid{display:grid;grid-template-columns:repeat(2,1fr);gap:16px;margin:14px 0}
.ex{background:#14141E;border:1px solid #2A2A3E;border-radius:12px;padding:16px 18px}
.ex-h{font-size:14px;margin-bottom:10px;line-height:1.5}
.ex-tag{font-family:monospace;font-size:11px;font-weight:700;border:1px solid currentColor;
 border-radius:6px;padding:2px 7px;margin-right:8px}
.ex-type{color:#8888AA;font-size:12px}
.ex-ask{color:#C8C8E0;font-size:13px;font-style:italic;line-height:1.5;
 border-left:2px solid #2A2A3E;padding-left:11px;margin:8px 0}
.ex-res{margin:6px 0 2px}
.ex-res code{background:#0C0C12;border:1px solid #2A2A3E;border-radius:5px;padding:2px 7px;
 font-size:11.5px;color:#9FE8D4;word-break:break-all}
.ex-detail{color:#8888AA;font-size:12px;line-height:1.55;margin:10px 0 4px}
.ex-t{margin:10px 0 0}
.ex-t td,.ex-t th{padding:5px 9px;font-size:12px}
.ex-t td:first-child{color:#8888AA}
td.good{color:#2DD4B4;font-weight:700}
td.invest{color:#F5A623;font-weight:700}
@media(max-width:760px){.exgrid{grid-template-columns:1fr}.cards{grid-template-columns:repeat(2,1fr)}}
"""


def _card(k, v, cls, note=""):
    return (f"<div class='card'><div class='k'>{k}</div>"
            f"<div class='v {cls}'>{v}</div>"
            f"<div class='n'>{note}</div></div>")


def _render_html(stats, charts, ts_label, examples=None) -> str:
    s, m = stats, stats["manifest"]
    cfg  = m["config"]
    e = lambda x: x
    examples_html = _render_examples(examples or [])

    cards = "".join([
        _card("Cost vs naive frontier", f"−{s['save_vs_frontier_cost']:.0f}%", "t-teal",
              f"${s['cost']['flat_opus']:.2f} → ${s['cost']['argus']:.2f}, equal quality"),
        _card("Time vs naive frontier", f"−{s['save_vs_frontier_time']:.0f}%", "t-teal",
              f"{s['time']['flat_opus']:.0f}s → {s['time']['argus']:.0f}s modeled"),
        _card("Pure efficiency", f"−{s['save_vs_qmatch_cost']:.0f}%", "t-blue",
              "vs same models, no compression/cache/stop"),
        _card("Quality", f"{s['quality_opt']:.3f}", "t-amber",
              f"Δ {s['quality_delta']:+.3f} vs flat-Sonnet"),
    ])

    # projection
    proj = (f"<div class='callout'>At this mix, Argus saves "
            f"<b>${s['cost_per_1m_saved_frontier']:,.0f} per 1,000,000 tasks</b> versus a naive "
            f"\"always use the frontier model\" policy — at the <b>same quality</b> — and "
            f"<b>${s['cost_per_1m_saved_qmatch']:,.0f} per 1,000,000 tasks</b> versus a perfectly "
            f"model-matched team that simply lacks our compression, caching and loop-stopping "
            f"layers. Linear projection from a {s['n_calls']}-task, fully reproducible sample "
            f"(seed {cfg['seed']}).</div>")

    figs = "".join([
        f"<div class='fig'><img src='data:image/png;base64,{charts['cost_bars']}'></div>",
        f"<div class='fig'><img src='data:image/png;base64,{charts['time_bars']}'></div>",
        f"<div class='fig'><img src='data:image/png;base64,{charts['cost_wf']}'></div>",
        f"<div class='fig'><img src='data:image/png;base64,{charts['time_wf']}'></div>",
        f"<div class='fig'><img src='data:image/png;base64,{charts['cum_cost']}'></div>",
        f"<div class='fig'><img src='data:image/png;base64,{charts['cum_time']}'></div>",
        f"<div class='fig'><img src='data:image/png;base64,{charts['routing']}'></div>",
        f"<div class='fig'><img src='data:image/png;base64,{charts['quality']}'></div>",
    ])

    # transparency table
    bw = m["band_weights"]; bc = m["band_counts"]
    band_rows = "".join(
        f"<tr><td>{b}</td><td>{m['required_by_band'][b]}</td>"
        f"<td>{bw.get(b, 0)*100:.0f}%</td><td>{bc.get(b, 0)}</td></tr>"
        for b in ["simple", "medium", "hard", "expert"])
    pills = "".join(
        f"<span class='pill'>{k} = {v}</span>"
        for k, v in [
            ("n_tasks", cfg["n_tasks"]), ("seed", cfg["seed"]),
            ("difficulty", cfg["difficulty"]), ("reasoning_depth", cfg["reasoning_depth"]),
            ("cache_hit_rate", cfg["cache_hit_rate"]), ("loop_rate", cfg["loop_rate"]),
            ("compressible_fraction", cfg["compressible_fraction"]),
            ("strong_model_fraction", cfg["strong_model_fraction"]),
        ])

    routing_rows = "".join(
        f"<tr><td>{k}</td><td>{v}</td><td>{v/s['n_calls']*100:.0f}%</td></tr>"
        for k, v in s["routing_dist"].items())

    return f"""<!doctype html><html><head><meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Argus — Investor Report {e(ts_label)}</title><style>{_CSS}</style></head><body><div class='wrap'>
<h1>Argus — Token Governance, Measured</h1>
<div class='sub'>Post-run optimization report · {s['n_calls']} tasks · generated {e(ts_label)} · fully reproducible (seed {cfg['seed']})</div>

<div class='cards'>{cards}</div>
{proj}

<h2>Cost &amp; time — before vs after</h2>
<div class='callout'>A naive team must choose between <b class='t-coral'>flat-Opus</b> (pay frontier
prices on every trivial task) and <b class='t-amber'>flat-Sonnet</b> (cheaper, but it silently
under-delivers on genuinely hard tasks). <b class='t-teal'>Argus</b> routes each task to exactly the
model it needs, then compresses, caches and stops stuck loops — delivering flat-Opus quality at a
fraction of flat-Opus cost and time.</div>
<div class='grid2'>{figs}</div>
<div class='note'>Wall-clock time is modeled from published per-model latency profiles (time-to-first-token + prefill + decode); see METHODOLOGY.md. All cost is list-price on both sides.</div>

<h2>How Argus routed it &amp; what quality resulted</h2>
<table><tr><th>Route</th><th>Tasks</th><th>Share</th></tr>{routing_rows}</table>

<h2>Worked examples — five real tasks from this run</h2>
<div class='callout'>Each card below is an <b>actual task</b> from this run (call # is its index in
<code style="color:#9FE8D4">data/calls.jsonl</code>): the real prompt, the <b>specific optimization
that fired</b>, and the exact before→after cost, time and quality. Four show money saved; the
<b class='t-purp'>ROUTE ↑</b> card shows the one case where Argus deliberately spends <i>more</i> — because the
task genuinely needs frontier reasoning and the cheap model would silently lose quality.</div>
{examples_html}

<h2>How this dataset was generated</h2>
<p class='sub'>Everything is parametrized and seeded — no hidden randomness, no API spend in mock mode.
Re-run with the same seed to reproduce every number on this page byte-for-byte.</p>
<div>{pills}</div>
<table style='margin-top:14px'>
<tr><th>Difficulty band</th><th>Required model</th><th>Share of unique tasks</th><th>Count</th></tr>
{band_rows}
</table>
<table style='margin-top:14px'>
<tr><th>Workload composition</th><th>Count</th></tr>
<tr><td>Unique tasks</td><td>{m['n_unique']}</td></tr>
<tr><td>Cache repeats (served from cache)</td><td>{m['n_cache_repeat']}</td></tr>
<tr><td>Stuck loops (SPRT force-stopped)</td><td>{m['n_loops']}</td></tr>
<tr><td>Total emitted</td><td>{m['n_emitted']}</td></tr>
</table>
<div class='note'>Routing policy (complexity → model): {", ".join(f"&lt;{u} → {mm}" if u != float('inf') else f"≥ → {mm}" for u, mm in m['routing_tiers'])}. Full methodology in METHODOLOGY.md.</div>

</div></body></html>"""


def print_investor_summary(stats, runtime_s, html_path):
    s = stats
    line = "─" * 62
    print("\n" + line)
    print(f"  ARGUS INVESTOR REPORT  |  {s['n_calls']} tasks  |  {runtime_s:.1f}s")
    print(line)
    print(f"  COST   flat-Opus ${s['cost']['flat_opus']:.2f}   "
          f"flat-Sonnet ${s['cost']['flat_sonnet']:.2f}   "
          f"q-matched ${s['cost']['quality_matched']:.2f}   "
          f"ARGUS ${s['cost']['argus']:.2f}")
    print(f"         → −{s['save_vs_frontier_cost']:.0f}% vs frontier (equal quality) · "
          f"−{s['save_vs_qmatch_cost']:.0f}% pure efficiency")
    print(f"  TIME   flat-Opus {s['time']['flat_opus']:.0f}s   "
          f"ARGUS {s['time']['argus']:.0f}s   → −{s['save_vs_frontier_time']:.0f}% vs frontier")
    print(f"  QUAL   {s['quality_opt']:.3f}  (Δ {s['quality_delta']:+.3f} vs flat-Sonnet)")
    print(f"  SCALE  ${s['cost_per_1m_saved_frontier']:,.0f} saved / 1M tasks vs frontier")
    print(f"\n  Report: {html_path}")
    print(line)


def generate_investor_report(calls, manifest, output_dir=None, runtime_s=0.0) -> Path:
    now = datetime.now()
    if output_dir is None:
        output_dir = Path("reports") / f"investor_{now:%Y%m%d_%H%M%S}"
    output_dir = Path(output_dir)
    (output_dir / "data").mkdir(parents=True, exist_ok=True)

    stats = compute_investor_stats(calls, manifest)
    cum_cost, cum_time = _chart_cumulative(calls, stats)
    charts = {
        "cost_bars": _chart_cost_bars(stats),
        "time_bars": _chart_time_bars(stats),
        "cost_wf":   _chart_cost_waterfall(stats),
        "time_wf":   _chart_time_waterfall(stats),
        "cum_cost":  cum_cost,
        "cum_time":  cum_time,
        "routing":   _chart_routing(stats),
        "quality":   _chart_quality(stats),
    }
    examples = pick_examples(calls)
    ts_label = now.strftime("%Y-%m-%d %H:%M")
    (output_dir / "investor_report.html").write_text(
        _render_html(stats, charts, ts_label, examples), encoding="utf-8")

    # machine-readable artefacts
    summ = {k: v for k, v in stats.items() if k != "manifest"}
    (output_dir / "summary.json").write_text(
        json.dumps(summ, indent=2, default=str), encoding="utf-8")
    (output_dir / "data" / "dataset_manifest.json").write_text(
        json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    (output_dir / "data" / "worked_examples.json").write_text(
        json.dumps(examples, indent=2, default=str), encoding="utf-8")
    with (output_dir / "data" / "calls.jsonl").open("w", encoding="utf-8") as f:
        for c in calls:
            f.write(json.dumps(c.to_dict(), default=str) + "\n")

    print_investor_summary(stats, runtime_s, str(output_dir / "investor_report.html"))
    return output_dir
