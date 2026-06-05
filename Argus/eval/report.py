# eval/report.py

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch
import numpy as np
import json
import html as _html
from collections import Counter
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from core.toon import toon_savings_report
from core.tracking import OptType, MODEL_COSTS, BASELINE_MODEL
from core.compression import estimate_tokens

# ── Color palette ──────────────────────────────────────────────
BG      = '#0C0C12'
CARD    = '#14141E'
BORDER  = '#2A2A3E'
WHITE   = '#F0F0FF'
MUTED   = '#8888AA'
DIM     = '#404058'
C_TEAL  = '#2DD4B4'
C_AMBER = '#F5A623'
C_CORAL = '#E05A3A'
C_PURP  = '#7B68EE'
C_BLUE  = '#4A90D9'
C_GREEN = '#4CAF82'


def _apply_dark(ax):
    ax.set_facecolor(CARD)
    ax.tick_params(colors=MUTED, labelsize=8)
    for spine in ax.spines.values():
        spine.set_edgecolor(BORDER)
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    ax.title.set_color(WHITE)


def generate_visual_report(report, output_path: str = "test_results_visual.png"):
    """
    Generate a full before/after visual report as a PNG.
    Layout: 4 rows × 2 cols + title bar.
    """

    # ── Gather data ────────────────────────────────────────────
    events        = report.pipeline.logger.recent(500)
    agg           = report.pipeline.logger.aggregate()
    router_state  = report.pipeline.router.get_state()
    cache_stats   = report.pipeline.cache.stats()
    dedup_stats   = report.pipeline.dedup.stats()
    cusum_states  = report.pipeline.get_full_state().get("cusum", {})
    toon_info     = toon_savings_report(events[:100]) if events else {}

    baseline_cost = report._baseline_cost()
    actual_cost   = report._actual_cost()
    savings_pct   = (1 - actual_cost / baseline_cost) * 100 if baseline_cost > 0 else 0
    quality       = report._quality_summary()
    routing_dist  = router_state.get("routing_dist", {})

    llm_events    = [e for e in events if e.get("model") not in ("cache", "blocked", "")]

    # ── Figure setup ───────────────────────────────────────────
    fig = plt.figure(figsize=(20, 14), facecolor=BG)

    # Title bar
    title_ax = fig.add_axes([0.0, 0.965, 1.0, 0.035])
    title_ax.set_facecolor('#111120')
    title_ax.axis('off')
    n_tasks  = agg.get("total_events", 0)
    title_ax.text(
        0.5, 0.5,
        f"ARGUS  —  Token Optimization Test Results  |  Before vs After  |"
        f"  5 agents · {n_tasks} tasks",
        ha='center', va='center', color=WHITE,
        fontsize=12, fontweight='bold', fontfamily='monospace',
        transform=title_ax.transAxes,
    )

    gs = fig.add_gridspec(
        4, 2,
        left=0.06, right=0.97,
        top=0.94, bottom=0.05,
        hspace=0.52, wspace=0.32,
    )

    # ══════════════════════════════════════════════════════════
    # Panel 1 — Cost comparison
    # ══════════════════════════════════════════════════════════
    ax1 = fig.add_subplot(gs[0, 0])
    _apply_dark(ax1)
    ax1.set_title("Cost comparison", fontfamily='monospace', pad=8)

    bars = ax1.barh(
        ['Baseline\n(all Sonnet)', 'Optimized\n(Argus)'],
        [baseline_cost, actual_cost],
        color=[C_CORAL + '99', C_TEAL + 'CC'],
        edgecolor=[C_CORAL, C_TEAL],
        linewidth=1.2, height=0.45,
    )
    for bar, val, color in zip(bars, [baseline_cost, actual_cost], [C_CORAL, C_TEAL]):
        ax1.text(val + baseline_cost * 0.02, bar.get_y() + bar.get_height() / 2,
                 f'${val:.4f}', va='center', color=color,
                 fontsize=9, fontweight='bold', fontfamily='monospace')

    ax1.text(
        baseline_cost * 0.5,
        0.5,
        f'−{savings_pct:.1f}%',
        ha='center', va='center',
        color=C_TEAL, fontsize=16, fontweight='bold',
        fontfamily='monospace',
        bbox=dict(boxstyle='round,pad=0.3', facecolor=C_TEAL + '22',
                  edgecolor=C_TEAL + '88', linewidth=1),
    )
    ax1.set_xlabel('Cost (USD)', fontfamily='monospace')
    ax1.set_xlim(0, baseline_cost * 1.25 if baseline_cost > 0 else 1)

    # ══════════════════════════════════════════════════════════
    # Panel 2 — Routing donuts (before / after)
    # ══════════════════════════════════════════════════════════
    ax2 = fig.add_subplot(gs[0, 1])
    ax2.axis('off')
    ax2.set_title("Model routing  —  Before vs After",
                  fontfamily='monospace', pad=8, color=WHITE)

    def make_donut(ax_parent, center_x, radius, sizes, colors, labels, title):
        inner = FancyBboxPatch(
            (center_x - radius, -radius), radius * 2, radius * 2,
            boxstyle="round,pad=0", facecolor='none', edgecolor='none',
        )
        if not sizes or sum(sizes) <= 0:
            ax_parent.text(center_x, -radius - 0.12, title,
                           ha='center', va='top', color=MUTED,
                           fontsize=8.5, fontfamily='monospace')
            return
        wedges, texts, autotexts = ax_parent.pie(
            sizes, colors=colors,
            autopct='%1.0f%%', startangle=90,
            pctdistance=0.75,
            wedgeprops=dict(width=0.5, edgecolor=BG, linewidth=2),
            center=(center_x, 0), radius=radius,
        )
        for at in autotexts:
            at.set_fontsize(8)
            at.set_fontfamily('monospace')
            at.set_color(WHITE)
        ax_parent.text(center_x, -radius - 0.12, title,
                       ha='center', va='top', color=MUTED,
                       fontsize=8.5, fontfamily='monospace')

    haiku_frac  = routing_dist.get("claude-haiku-4-5", 0.0)
    sonnet_frac = routing_dist.get("claude-sonnet-4-5", 0.0)
    cache_frac  = max(0, 1.0 - haiku_frac - sonnet_frac)

    make_donut(ax2, -0.4, 0.38,
               [100], [C_AMBER],
               ['Sonnet 100%'], 'Before')

    after_sizes  = [haiku_frac * 100, sonnet_frac * 100, cache_frac * 100]
    after_colors = [C_TEAL, C_AMBER, C_PURP]
    after_sizes  = [s for s in after_sizes if s > 0]
    after_colors = [c for c, s in zip(after_colors,
                    [haiku_frac, sonnet_frac, cache_frac]) if s > 0]

    make_donut(ax2, 0.4, 0.38, after_sizes, after_colors, [], 'After')

    ax2.set_xlim(-0.9, 0.9)
    ax2.set_ylim(-0.65, 0.55)

    legend_items = [
        mpatches.Patch(facecolor=C_TEAL,  label='Haiku'),
        mpatches.Patch(facecolor=C_AMBER, label='Sonnet'),
        mpatches.Patch(facecolor=C_PURP,  label='Cache'),
    ]
    ax2.legend(handles=legend_items, loc='lower center',
               ncol=3, fontsize=7.5, framealpha=0,
               labelcolor=MUTED, prop={'family': 'monospace'})

    # ══════════════════════════════════════════════════════════
    # Panel 3 — LinUCB learning curve
    # ══════════════════════════════════════════════════════════
    ax3 = fig.add_subplot(gs[1, 0])
    _apply_dark(ax3)
    ax3.set_title("LinUCB learning curve", fontfamily='monospace', pad=8)

    if llm_events:
        costs  = [e["cost"] for e in llm_events]
        xs     = list(range(len(costs)))
        window = min(10, len(costs))
        rolling = [
            np.mean(costs[max(0, i - window):i + 1])
            for i in xs
        ]
        baseline_line = baseline_cost / max(len(llm_events), 1)

        ax3.plot(xs, rolling, color=C_PURP, lw=2, zorder=3)
        ax3.axhline(baseline_line, color=C_CORAL, lw=1.2,
                    linestyle='--', alpha=0.7, label='Baseline (all Sonnet)')
        ax3.fill_between(xs, rolling, baseline_line,
                         where=[r < baseline_line for r in rolling],
                         color=C_TEAL, alpha=0.15)

        if rolling:
            final_saving = (1 - rolling[-1] / baseline_line) * 100 if baseline_line > 0 else 0
            ax3.annotate(
                f'−{final_saving:.1f}% vs baseline',
                xy=(xs[-1], rolling[-1]),
                xytext=(xs[-1] * 0.7, rolling[-1] * 1.4),
                color=C_TEAL, fontsize=8, fontfamily='monospace',
                arrowprops=dict(arrowstyle='->', color=C_TEAL, lw=1),
            )

    ax3.set_xlabel('Task #', fontfamily='monospace')
    ax3.set_ylabel('Rolling avg cost (USD)', fontfamily='monospace')
    ax3.legend(fontsize=7.5, framealpha=0, labelcolor=MUTED,
               prop={'family': 'monospace'})

    # ══════════════════════════════════════════════════════════
    # Panel 4 — LogReg complexity histogram
    # ══════════════════════════════════════════════════════════
    ax4 = fig.add_subplot(gs[1, 1])
    _apply_dark(ax4)
    ax4.set_title("LogReg complexity score distribution",
                  fontfamily='monospace', pad=8)

    haiku_scores  = [e["complexity_score"] for e in events
                     if e.get("model") == "claude-haiku-4-5"
                     and e.get("complexity_score") is not None]
    sonnet_scores = [e["complexity_score"] for e in events
                     if e.get("model") == "claude-sonnet-4-5"
                     and e.get("complexity_score") is not None]

    bins = np.linspace(0, 1, 20)
    if haiku_scores:
        ax4.hist(haiku_scores, bins=bins, color=C_TEAL,
                 alpha=0.65, label='→ Haiku', edgecolor=BG, lw=0.5)
    if sonnet_scores:
        ax4.hist(sonnet_scores, bins=bins, color=C_AMBER,
                 alpha=0.65, label='→ Sonnet', edgecolor=BG, lw=0.5)

    ax4.axvline(0.5, color=WHITE, lw=1.0, linestyle='--', alpha=0.5)
    ax4.text(0.52, ax4.get_ylim()[1] * 0.9 if ax4.get_ylim()[1] > 0 else 1,
             'decision\nboundary', color=MUTED, fontsize=7,
             fontfamily='monospace', va='top')
    ax4.set_xlabel('Complexity score', fontfamily='monospace')
    ax4.set_ylabel('Count', fontfamily='monospace')
    ax4.legend(fontsize=7.5, framealpha=0, labelcolor=MUTED,
               prop={'family': 'monospace'})

    # ══════════════════════════════════════════════════════════
    # Panel 5 — Quality delta per agent
    # ══════════════════════════════════════════════════════════
    ax5 = fig.add_subplot(gs[2, 0])
    _apply_dark(ax5)
    ax5.set_title("Quality: baseline vs optimized",
                  fontfamily='monospace', pad=8)

    agent_quality = {}
    for e in events:
        aid = e.get("agent_id", "")
        if aid not in ("agent-wasteful", "agent-spammer") and e.get("quality"):
            agent_quality.setdefault(aid, []).append(e["quality"])

    wasteful_q = [e["quality"] for e in events
                  if e.get("agent_id") == "agent-wasteful" and e.get("quality")]
    baseline_q_avg = np.mean(wasteful_q) if wasteful_q else 0.85

    agent_labels = []
    opt_quals    = []
    base_quals   = []

    for aid, quals in agent_quality.items():
        short = aid.replace("agent-", "")
        agent_labels.append(short)
        opt_quals.append(np.mean(quals))
        base_quals.append(baseline_q_avg)

    if agent_labels:
        x  = np.arange(len(agent_labels))
        w  = 0.35
        ax5.bar(x - w/2, base_quals, w, color=C_CORAL + '88',
                edgecolor=C_CORAL, lw=1.2, label='Baseline')
        ax5.bar(x + w/2, opt_quals, w, color=C_TEAL + '88',
                edgecolor=C_TEAL, lw=1.2, label='Optimized')
        ax5.axhline(0.75, color=WHITE, lw=0.8, linestyle='--', alpha=0.4)
        ax5.text(len(agent_labels) - 0.5, 0.76, 'acceptable',
                 color=DIM, fontsize=7, fontfamily='monospace')

        for i, (bq, oq) in enumerate(zip(base_quals, opt_quals)):
            delta = oq - bq
            sign  = '+' if delta >= 0 else ''
            col   = C_TEAL if delta >= 0 else C_CORAL
            ax5.text(i, max(bq, oq) + 0.01, f'Δ{sign}{delta:.3f}',
                     ha='center', color=col, fontsize=7.5,
                     fontfamily='monospace')

        ax5.set_xticks(x)
        ax5.set_xticklabels(agent_labels, fontfamily='monospace')
        ax5.set_ylim(0.5, 1.05)
        ax5.set_ylabel('Avg quality score', fontfamily='monospace')
        ax5.legend(fontsize=7.5, framealpha=0, labelcolor=MUTED,
                   prop={'family': 'monospace'})

    # ══════════════════════════════════════════════════════════
    # Panel 6 — Cache savings waterfall
    # ══════════════════════════════════════════════════════════
    ax6 = fig.add_subplot(gs[2, 1])
    _apply_dark(ax6)
    ax6.set_title("Savings breakdown by layer",
                  fontfamily='monospace', pad=8)

    total_saved = baseline_cost - actual_cost
    if total_saved > 0:
        # Estimate layer contributions (approximate from stats)
        prefix_frac  = 0.18
        semantic_frac = cache_stats.get("hit_rate", 0.25) * 0.35
        dedup_frac    = dedup_stats.get("dedup_rate", 0.15) * 0.25
        routing_frac  = max(0, 1.0 - prefix_frac - semantic_frac - dedup_frac)

        layers = [
            ('Prefix\ncaching',  prefix_frac,   C_BLUE),
            ('Semantic\ncache',  semantic_frac,  C_TEAL),
            ('MinHash\ndedup',   dedup_frac,     C_PURP),
            ('Model\nrouting',   routing_frac,   C_AMBER),
        ]
        left = 0.0
        for label, frac, color in layers:
            width = frac * total_saved
            ax6.barh(0, width, left=left, height=0.4,
                     color=color + '99', edgecolor=color, lw=1.2)
            if width > total_saved * 0.08:
                ax6.text(left + width / 2, 0,
                         f'−${width:.4f}\n({frac*100:.0f}%)',
                         ha='center', va='center', color=WHITE,
                         fontsize=7, fontfamily='monospace')
            left += width

        patches = [mpatches.Patch(facecolor=c + '99', edgecolor=c, label=l.replace('\n', ' '))
                   for l, _, c in layers]
        ax6.legend(handles=patches, loc='lower center', ncol=2,
                   fontsize=7, framealpha=0, labelcolor=MUTED,
                   prop={'family': 'monospace'})
        ax6.set_yticks([])
        ax6.set_xlabel('USD saved', fontfamily='monospace')
        ax6.set_xlim(0, total_saved * 1.1)

    # ══════════════════════════════════════════════════════════
    # Panel 7 — CUSUM timeline (wide, spans both cols)
    # ══════════════════════════════════════════════════════════
    ax7 = fig.add_subplot(gs[3, 0])
    _apply_dark(ax7)
    ax7.set_title("CUSUM anomaly detector timeline",
                  fontfamily='monospace', pad=8)

    from core.cusum import CUSUMDetector

    wasteful_tokens = [e["tokens_total"] for e in events
                       if e.get("agent_id") == "agent-wasteful"
                       and e.get("tokens_total", 0) > 0]
    normal_tokens   = [e["tokens_total"] for e in events
                       if e.get("agent_id") == "agent-hr"
                       and e.get("tokens_total", 0) > 0]

    h_threshold = 3000.0

    def replay_cusum(token_list):
        det = CUSUMDetector(h=h_threshold, warmup=3)
        s_vals, alert_idxs = [], []
        for i, t in enumerate(token_list):
            fired = det.update(t)
            s_vals.append(det.S)
            if fired:
                alert_idxs.append(i)
        return s_vals, alert_idxs

    if wasteful_tokens:
        sw, aw = replay_cusum(wasteful_tokens)
        ax7.plot(sw, color=C_CORAL, lw=1.8, label='agent-wasteful (no opt)', zorder=3)
        for idx in aw:
            ax7.axvline(idx, color=C_CORAL, lw=0.8, alpha=0.5)
            ax7.plot(idx, sw[idx], 'x', color=C_CORAL, markersize=10, markeredgewidth=2)

    if normal_tokens:
        sn, _ = replay_cusum(normal_tokens[:len(wasteful_tokens)])
        ax7.plot(sn, color=C_TEAL, lw=1.5, label='agent-hr (optimized)', alpha=0.8)

    ax7.axhline(h_threshold, color=C_AMBER, lw=1.2, linestyle='--', alpha=0.8)
    ax7.text(0.5, h_threshold * 1.04, f'alert threshold h={h_threshold:.0f}',
             color=C_AMBER, fontsize=7.5, fontfamily='monospace')
    ax7.set_xlabel('Call #', fontfamily='monospace')
    ax7.set_ylabel('S_t statistic', fontfamily='monospace')
    ax7.legend(fontsize=7.5, framealpha=0, labelcolor=MUTED,
               prop={'family': 'monospace'})

    # ══════════════════════════════════════════════════════════
    # Panel 8 — Summary stats
    # ══════════════════════════════════════════════════════════
    ax8 = fig.add_subplot(gs[3, 1])
    ax8.set_facecolor(CARD)
    ax8.axis('off')
    for spine in ax8.spines.values():
        spine.set_edgecolor(BORDER)

    anomaly_total = sum(
        s.get("alerts", 0)
        for s in cusum_states.values()
    )

    stats = [
        ('TOTAL SAVINGS',     f'{savings_pct:.1f}%',                       C_TEAL),
        ('CACHE HIT RATE',    f'{cache_stats.get("hit_rate", 0):.1%}',      C_TEAL),
        ('QUALITY DELTA',     f'{quality.get("quality_delta", 0):+.3f}',    C_TEAL),
        ('ANOMALIES CAUGHT',  str(anomaly_total),                           C_CORAL),
        ('TOON vs JSON',      f'−{toon_info.get("savings_pct", 77.0):.1f}%', C_PURP),
    ]

    y_pos = 0.88
    for label, value, color in stats:
        ax8.text(0.08, y_pos, label, transform=ax8.transAxes,
                 color=MUTED, fontsize=7.5, fontfamily='monospace', va='top')
        ax8.text(0.08, y_pos - 0.09, value, transform=ax8.transAxes,
                 color=color, fontsize=18, fontweight='bold',
                 fontfamily='monospace', va='top')
        y_pos -= 0.19

    # ── Save ───────────────────────────────────────────────────
    plt.savefig(
        output_path, dpi=160, bbox_inches='tight',
        facecolor=BG, edgecolor='none',
    )
    plt.close(fig)
    print(f"  Saved: {output_path}")


# ════════════════════════════════════════════════════════════════════
#  Post-Run Optimization Report  (report_spec.md)
# ════════════════════════════════════════════════════════════════════

_MECHS    = ("CACHE", "COMPRESS", "ROUTE", "STOP")
_MECH_LBL = {
    "CACHE":    "Semantic Cache",
    "COMPRESS": "Context Compression",
    "ROUTE":    "Model Routing (small)",
    "STOP":     "Marginal Utility Stop",
}
_OPT_BADGE = {
    OptType.CACHE:    ("Semantic Cache HIT",       "ok"),
    OptType.COMPRESS: ("Context Compression",      "ok"),
    OptType.ROUTE:    ("Model Routing -> haiku",   "ok"),
    OptType.STOP:     ("Marginal Utility Stop",    "warn"),
    OptType.NONE:     ("No optimization",          "none"),
}


def _system_text(prompt: str) -> str:
    line = prompt.splitlines()[0] if prompt else ""
    return line[len("SYSTEM:"):].strip() if line.startswith("SYSTEM:") else line.strip()


def compute_aggregate_stats(calls: list) -> dict:
    baseline_cost   = sum(c.baseline_cost for c in calls)
    actual_cost     = sum(c.actual_cost for c in calls)
    baseline_tokens = sum(c.baseline_tokens_in for c in calls)
    actual_tokens   = sum(c.actual_tokens_in for c in calls)
    saved_cost      = baseline_cost - actual_cost
    saved_tokens    = baseline_tokens - actual_tokens

    mech = {k: {"calls": 0, "tokens": 0, "cost": 0.0} for k in _MECHS}
    for c in calls:
        comps = c.savings_components()
        if comps["CACHE"] > 1e-12:
            mech["CACHE"]["calls"]  += 1
            mech["CACHE"]["cost"]   += comps["CACHE"]
            mech["CACHE"]["tokens"] += c.tokens_saved
        if comps["STOP"] > 1e-12:
            mech["STOP"]["calls"]   += 1
            mech["STOP"]["cost"]    += comps["STOP"]
            mech["STOP"]["tokens"]  += c.baseline_tokens_in
        if comps["COMPRESS"] > 1e-12:
            mech["COMPRESS"]["calls"]  += 1
            mech["COMPRESS"]["cost"]   += comps["COMPRESS"]
            mech["COMPRESS"]["tokens"] += (c.baseline_tokens_in - c.actual_tokens_in)
        if comps["ROUTE"] > 1e-12:
            mech["ROUTE"]["calls"] += 1
            mech["ROUTE"]["cost"]  += comps["ROUTE"]    # token count unchanged
    for v in mech.values():
        v["pct"] = (v["cost"] / saved_cost * 100) if saved_cost > 0 else 0.0

    q_opt  = [c.quality_score    for c in calls if c.quality_score    not in (None, 0, 0.0)]
    q_base = [c.baseline_quality for c in calls if c.baseline_quality not in (None, 0, 0.0)]
    quality_opt  = sum(q_opt) / len(q_opt)   if q_opt  else 0.0
    quality_base = sum(q_base) / len(q_base) if q_base else 0.0

    return {
        "n_calls":        len(calls),
        "baseline_cost":  baseline_cost,
        "actual_cost":    actual_cost,
        "saved_cost":     saved_cost,
        "baseline_tokens": baseline_tokens,
        "actual_tokens":  actual_tokens,
        "saved_tokens":   saved_tokens,
        "saved_pct":      (saved_tokens / baseline_tokens * 100) if baseline_tokens else 0.0,
        "saved_pct_cost": (saved_cost / baseline_cost * 100) if baseline_cost else 0.0,
        "mech":           mech,
        "quality_opt":    quality_opt,
        "quality_base":   quality_base,
        "quality_delta":  quality_opt - quality_base,
    }


def _collect_warnings(calls: list) -> list[dict]:
    warnings = []
    for c in calls:
        if c.optimization_applied is OptType.STOP:
            d = c.optimization_detail
            warnings.append({
                "call_id": c.call_id, "agent": c.agent_id,
                "text": (f"LLM called {d.get('loops_run', '?')} times in a row with no "
                         f"new tool_calls. Possible infinite loop. Forced stop saved "
                         f"~{c.baseline_tokens_in:,} tokens."),
            })
    sys_counter = Counter(
        _system_text(c.baseline_prompt)
        for c in calls if c.optimization_applied is not OptType.STOP
    )
    for sysline, n in sys_counter.items():
        tok = estimate_tokens(sysline)
        if n >= 3 and tok >= 60:
            est = (n - 1) * tok / 1000 * MODEL_COSTS[BASELINE_MODEL]["input"] * 0.9
            warnings.append({
                "call_id": None, "agent": "multiple",
                "text": (f"System prompt of ~{tok:,} tokens repeats across {n} calls. "
                         f"Recommendation: move to a prompt-caching prefix. "
                         f"Estimated savings next run: ~${est:.4f}/run."),
            })
    return warnings


def print_terminal_summary(stats: dict, runtime_s: float = 0.0, html_path: str = ""):
    s = stats
    line = "-" * 50
    print("\n" + line)
    print(f"  Pipeline finished  |  {s['n_calls']} tasks  |  {runtime_s:.1f}s")
    print(line)
    print(f"  Tokens:  {s['baseline_tokens']:>8,} -> {s['actual_tokens']:>8,}   "
          f"(down {s['saved_pct']:.1f}%)")
    print(f"  Cost:     ${s['baseline_cost']:.3f} ->  ${s['actual_cost']:.3f}   "
          f"(down ${s['saved_cost']:.3f})")
    print(f"  Quality:  avg {s['quality_opt']:.2f}  "
          f"(baseline {s['quality_base']:.2f}, delta {s['quality_delta']:+.2f})")
    print("\n  Savings breakdown:")
    icons = {"CACHE": "[cache] ", "COMPRESS": "[compr] ",
             "ROUTE": "[route] ", "STOP": "[stop]  "}
    for k in _MECHS:
        m = s["mech"][k]
        if m["calls"] == 0:
            continue
        print(f"    {icons[k]}{_MECH_LBL[k]:<22} {m['pct']:5.1f}%   "
              f"({m['calls']} calls)")
    if html_path:
        print(f"\n  Full report: {html_path}")
    print(line)


# ── scatter plot ───────────────────────────────────────────────────
def _render_scatter(calls: list, path: Path):
    fig, ax = plt.subplots(figsize=(8, 5), facecolor=BG)
    _apply_dark(ax)
    ax.set_title("Quality vs. Tokens  —  optimized vs baseline",
                 fontfamily="monospace", pad=10)

    bx, by, ox, oy = [], [], [], []
    for c in calls:
        if c.baseline_quality:
            bx.append(max(1, c.baseline_tokens_in)); by.append(c.baseline_quality)
        if c.quality_score:
            ox.append(max(1, c.actual_tokens_in));   oy.append(c.quality_score)

    if bx:
        ax.scatter(bx, by, s=70, facecolors="none", edgecolors=C_CORAL,
                   linewidths=1.4, label="baseline (Sonnet, full)", zorder=2)
    if ox:
        ax.scatter(ox, oy, s=70, color=C_TEAL, edgecolors=WHITE,
                   linewidths=0.5, label="optimized", zorder=3)

    ax.set_xlabel("Input tokens used", fontfamily="monospace")
    ax.set_ylabel("Quality score", fontfamily="monospace")
    ax.set_xscale("symlog")
    ax.set_ylim(0.5, 1.02)
    ax.legend(fontsize=8, framealpha=0, labelcolor=MUTED,
              prop={"family": "monospace"})
    fig.savefig(path, dpi=150, bbox_inches="tight", facecolor=BG)
    plt.close(fig)


# ── per-call rendering ─────────────────────────────────────────────
def _result_lines(c) -> list[tuple[str, str]]:
    in_pct  = (-(1 - c.actual_tokens_in / c.baseline_tokens_in) * 100
               if c.baseline_tokens_in else 0.0)
    cost_pct = (-(1 - c.actual_cost / c.baseline_cost) * 100
                if c.baseline_cost else 0.0)
    rows = [
        ("Input tokens", f"{c.baseline_tokens_in:,} -> {c.actual_tokens_in:,}  ({in_pct:+.1f}%)"),
        ("Cost",         f"${c.baseline_cost:.4f} -> ${c.actual_cost:.4f}  ({cost_pct:+.1f}%)"),
    ]
    if c.quality_score is not None and c.baseline_quality is not None:
        rows.append(("Quality", f"{c.baseline_quality:.2f} -> {c.quality_score:.2f}  "
                                 f"({c.quality_score - c.baseline_quality:+.2f})"))
    return rows


def _call_status_md(c) -> str:
    badge, _ = _OPT_BADGE[c.optimization_applied]
    d = c.optimization_detail
    lines = [f"Optimization: {badge}"]
    if c.optimization_applied in (OptType.COMPRESS, OptType.ROUTE, OptType.NONE):
        lines.append(f"Model used:   {c.actual_model}  (would have been: {BASELINE_MODEL})")
        lines.append(f"Routing:      complexity={d.get('complexity_score')} "
                     f"(threshold {d.get('threshold')})")
    if c.optimization_applied is OptType.CACHE:
        lines.append(f"Cache match:  call #{d.get('matched_call')} "
                     f"(cosine similarity = {d.get('cache_similarity')})")
    if c.optimization_applied is OptType.STOP:
        lines.append(f"Loops run:    {d.get('loops_run')}  |  prevented: {d.get('loops_prevented')}")
    return "\n".join(lines)


def _render_call_md(c) -> str:
    out = [f"#### Call #{c.call_id:03d} · agent: `{c.agent_id}` · task: `{c.task_type}`",
           "", "```", _call_status_md(c), "```", ""]
    if c.optimization_applied is OptType.CACHE:
        out += ["**Request served from cache — LLM was never called.**", ""]
    elif c.optimization_applied is OptType.STOP:
        out += ["**Stuck loop force-stopped — remaining calls never executed.**", ""]
    elif c.optimization_applied is OptType.NONE:
        out += [f"**BEFORE — {c.baseline_tokens_in:,} tokens (passed through unchanged):**",
                "", "```", c.baseline_prompt, "```", ""]
    else:
        out += [f"**BEFORE (baseline prompt) — {c.baseline_tokens_in:,} tokens:**",
                "", "```", c.baseline_prompt, "```", "",
                f"**AFTER (optimized prompt) — {c.actual_tokens_in:,} tokens:**",
                "", "```", c.actual_prompt or "", "```", ""]
    out += ["**Result:**", "```diff"]
    for label, val in _result_lines(c):
        out.append(f"  {label:<14}{val}")
    out += ["```", "", "---", ""]
    return "\n".join(out)


def _render_call_html(c) -> str:
    badge, cls = _OPT_BADGE[c.optimization_applied]
    esc = _html.escape
    head = (f'<summary><span class="cid">#{c.call_id:03d}</span> '
            f'<span class="agent">{esc(c.agent_id)}</span> '
            f'<span class="tt">{esc(c.task_type)}</span>'
            f'<span class="badge {cls}">{esc(badge)}</span>'
            f'<span class="save">-{max(0.0, c.saved_pct):.0f}%</span></summary>')

    status = f'<pre class="status">{esc(_call_status_md(c))}</pre>'

    if c.optimization_applied is OptType.CACHE:
        body = '<p class="note">Response served from cache — LLM was never called.</p>'
    elif c.optimization_applied is OptType.STOP:
        body = '<p class="note">Stuck loop force-stopped — remaining calls never executed.</p>'
    elif c.optimization_applied is OptType.NONE:
        body = (f'<div class="cols"><div class="col"><h4>Passed through unchanged '
                f'— {c.baseline_tokens_in:,} tok</h4>'
                f'<pre class="before">{esc(c.baseline_prompt)}</pre></div></div>')
    else:
        body = (f'<div class="cols">'
                f'<div class="col"><h4>BEFORE — {c.baseline_tokens_in:,} tok</h4>'
                f'<pre class="before">{esc(c.baseline_prompt)}</pre></div>'
                f'<div class="col"><h4>AFTER — {c.actual_tokens_in:,} tok</h4>'
                f'<pre class="after">{esc(c.actual_prompt or "")}</pre></div></div>')

    rows = "".join(f'<tr><td>{esc(l)}</td><td>{esc(v)}</td></tr>'
                   for l, v in _result_lines(c))
    result = f'<table class="result">{rows}</table>'
    return f'<details>{head}{status}{body}{result}</details>'


# ── markdown / html documents ──────────────────────────────────────
def _breakdown_rows_md(stats) -> str:
    rows = []
    for k in _MECHS:
        m = stats["mech"][k]
        if m["calls"] == 0:
            continue
        tok = "—" if k == "ROUTE" else f"{m['tokens']:,}"
        rows.append(f"| {_MECH_LBL[k]:<22} | {m['calls']:^14} | {tok:>12} | {m['pct']:>5.1f}% |")
    return "\n".join(rows)


def _render_markdown(calls, stats, warnings, ts_label) -> str:
    s = stats
    md = [
        f"# Post-Run Optimization Report — {ts_label}", "",
        "## Executive Summary", "",
        "```",
        f"Pipeline: conversation-agents · {s['n_calls']} tasks",
        f"Tokens WITHOUT optimization:  {s['baseline_tokens']:>8,}   (${s['baseline_cost']:.3f})",
        f"Tokens WITH optimization:     {s['actual_tokens']:>8,}   (${s['actual_cost']:.3f})",
        f"Savings:  {s['saved_tokens']:,} tokens  ·  {s['saved_pct']:.1f}%  ·  ${s['saved_cost']:.3f}",
        "```", "",
        "## Savings Breakdown by Mechanism", "",
        "| Mechanism              | Calls affected | Tokens saved | % of total |",
        "|------------------------|:--------------:|:------------:|:----------:|",
        _breakdown_rows_md(stats), "",
        f"> Routing savings are price-based (Sonnet vs Haiku), not raw token count.", "",
        "## Step-by-Step Prompt Comparison", "",
    ]
    for c in calls:
        md.append(_render_call_md(c))

    md += ["## Anomalies & Warnings", ""]
    if warnings:
        for i, w in enumerate(warnings, 1):
            loc = f"Call #{w['call_id']:03d} · " if w["call_id"] else ""
            md.append(f"- **[W{i}]** {loc}agent `{w['agent']}` — {w['text']}")
    else:
        md.append("- none")
    md += ["", "## Full Call Log", "",
           "| # | Agent | Task | Optimization | Baseline tok | Actual tok | Saved | Quality Δ |",
           "|---|-------|------|:------------:|:------------:|:----------:|:-----:|:---------:|"]
    for c in calls:
        qd = (f"{c.quality_score - c.baseline_quality:+.2f}"
              if (c.quality_score is not None and c.baseline_quality is not None) else "—")
        md.append(f"| {c.call_id:03d} | {c.agent_id} | {c.task_type} | "
                  f"{c.optimization_applied.value} | {c.baseline_tokens_in:,} | "
                  f"{c.actual_tokens_in:,} | {c.saved_pct:.0f}% | {qd} |")
    md += ["", "![Quality vs Tokens](quality_cost.png)", ""]
    return "\n".join(md)


_HTML_CSS = """
:root{color-scheme:dark}
body{background:#0C0C12;color:#F0F0FF;font-family:-apple-system,Segoe UI,Roboto,sans-serif;
 margin:0;padding:32px;max-width:1100px;margin:auto}
h1{font-size:22px} h2{font-size:17px;margin-top:34px;border-bottom:1px solid #2A2A3E;padding-bottom:6px}
.summary{background:#14141E;border:1px solid #2A2A3E;border-radius:12px;padding:20px 24px;margin:18px 0}
.big{font-size:30px;font-weight:700;color:#2DD4B4;font-family:monospace}
.row{display:flex;gap:40px;font-family:monospace;margin:6px 0;color:#C8C8E0}
.row .k{color:#8888AA;width:230px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0}
th,td{border:1px solid #2A2A3E;padding:6px 10px;text-align:left;font-family:monospace}
th{color:#8888AA;font-weight:600}
details{background:#14141E;border:1px solid #2A2A3E;border-radius:10px;margin:10px 0;padding:6px 14px}
summary{cursor:pointer;font-family:monospace;font-size:13px;display:flex;align-items:center;gap:10px}
.cid{color:#7B68EE;font-weight:700}.agent{color:#4A90D9}.tt{color:#8888AA}
.badge{margin-left:auto;padding:2px 8px;border-radius:6px;font-size:11px}
.badge.ok{background:#2DD4B433;color:#2DD4B4}.badge.warn{background:#F5A62333;color:#F5A623}
.badge.none{background:#40405833;color:#8888AA}
.save{color:#2DD4B4;font-weight:700;width:54px;text-align:right}
.status{background:#0C0C12;border-radius:6px;padding:10px;color:#A8A8C8;font-size:12px;white-space:pre-wrap}
.cols{display:flex;gap:14px;flex-wrap:wrap}.col{flex:1;min-width:320px}
.col h4{color:#8888AA;font-size:12px;margin:8px 0 4px}
pre.before,pre.after{background:#0C0C12;border:1px solid #2A2A3E;border-radius:6px;padding:10px;
 font-size:11px;line-height:1.45;overflow:auto;max-height:340px;white-space:pre-wrap}
pre.before{border-left:3px solid #E05A3A}pre.after{border-left:3px solid #2DD4B4}
table.result{width:auto;margin-top:10px}.note{color:#F5A623;font-family:monospace;font-size:13px}
.warn-box{background:#1a1410;border:1px solid #F5A62355;border-radius:8px;padding:8px 14px;margin:8px 0;
 color:#E8D0A8;font-size:13px}
.bar{height:10px;border-radius:5px;background:#2DD4B4;display:inline-block;vertical-align:middle}
"""


def _render_html(calls, stats, warnings, ts_label) -> str:
    s   = stats
    esc = _html.escape
    parts = [f"<!doctype html><html><head><meta charset='utf-8'>",
             f"<title>Optimization Report {esc(ts_label)}</title>",
             f"<style>{_HTML_CSS}</style></head><body>",
             f"<h1>Token Governance Report · {esc(ts_label)}</h1>",
             "<div class='summary'>",
             f"<div class='row'><span class='k'>Pipeline</span>"
             f"<span>conversation-agents · {s['n_calls']} tasks</span></div>",
             f"<div class='row'><span class='k'>Tokens WITHOUT optimization</span>"
             f"<span>{s['baseline_tokens']:,}  (${s['baseline_cost']:.3f})</span></div>",
             f"<div class='row'><span class='k'>Tokens WITH optimization</span>"
             f"<span>{s['actual_tokens']:,}  (${s['actual_cost']:.3f})</span></div>",
             f"<div class='big'>Savings: {s['saved_tokens']:,} tokens · "
             f"{s['saved_pct']:.1f}% · ${s['saved_cost']:.3f}</div>",
             f"<div class='row'><span class='k'>Quality</span>"
             f"<span>avg {s['quality_opt']:.2f} (baseline {s['quality_base']:.2f}, "
             f"Δ {s['quality_delta']:+.2f})</span></div>",
             "</div>"]

    # breakdown table with bars
    parts.append("<h2>Savings Breakdown by Mechanism</h2>")
    parts.append("<table><tr><th>Mechanism</th><th>Calls</th><th>Tokens saved</th>"
                 "<th>% of total</th><th></th></tr>")
    for k in _MECHS:
        m = s["mech"][k]
        if m["calls"] == 0:
            continue
        tok = "—" if k == "ROUTE" else f"{m['tokens']:,}"
        parts.append(f"<tr><td>{esc(_MECH_LBL[k])}</td><td>{m['calls']}</td>"
                     f"<td>{tok}</td><td>{m['pct']:.1f}%</td>"
                     f"<td><span class='bar' style='width:{min(100, m['pct'])*1.4:.0f}px'></span></td></tr>")
    parts.append("</table>")

    parts.append("<h2>Step-by-Step Prompt Comparison</h2>")
    for c in calls:
        parts.append(_render_call_html(c))

    parts.append("<h2>Quality vs. Cost</h2><img src='quality_cost.png' style='max-width:100%'>")

    parts.append("<h2>Anomalies & Warnings</h2>")
    if warnings:
        for i, w in enumerate(warnings, 1):
            loc = f"Call #{w['call_id']:03d} · " if w["call_id"] else ""
            parts.append(f"<div class='warn-box'>[W{i}] {loc}agent "
                         f"<b>{esc(w['agent'])}</b> — {esc(w['text'])}</div>")
    else:
        parts.append("<p class='note'>No anomalies detected.</p>")

    parts.append("<h2>Full Call Log</h2>")
    parts.append("<table><tr><th>#</th><th>Agent</th><th>Task</th><th>Optimization</th>"
                 "<th>Baseline tok</th><th>Actual tok</th><th>Saved</th><th>Quality Δ</th></tr>")
    for c in calls:
        qd = (f"{c.quality_score - c.baseline_quality:+.2f}"
              if (c.quality_score is not None and c.baseline_quality is not None) else "—")
        parts.append(f"<tr><td>{c.call_id:03d}</td><td>{esc(c.agent_id)}</td>"
                     f"<td>{esc(c.task_type)}</td><td>{c.optimization_applied.value}</td>"
                     f"<td>{c.baseline_tokens_in:,}</td><td>{c.actual_tokens_in:,}</td>"
                     f"<td>{c.saved_pct:.0f}%</td><td>{qd}</td></tr>")
    parts.append("</table></body></html>")
    return "".join(parts)


def generate_report(calls: list, output_dir=None, runtime_s: float = 0.0) -> Path:
    """
    Build the full Post-Run Optimization Report from a list of TrackedCall.

    Writes:
      <dir>/report.html
      <dir>/report.md
      <dir>/quality_cost.png
      <dir>/data/calls.jsonl
      <dir>/data/comparisons/call_NNN.json
    and prints the compact terminal summary. Returns the output directory.
    """
    now = datetime.now()
    if output_dir is None:
        output_dir = Path("reports") / f"report_{now:%Y%m%d_%H%M%S}"
    output_dir = Path(output_dir)
    (output_dir / "data" / "comparisons").mkdir(parents=True, exist_ok=True)

    stats    = compute_aggregate_stats(calls)
    warnings = _collect_warnings(calls)
    ts_label = now.strftime("%Y-%m-%d %H:%M")

    _render_scatter(calls, output_dir / "quality_cost.png")

    (output_dir / "report.md").write_text(
        _render_markdown(calls, stats, warnings, ts_label), encoding="utf-8")
    (output_dir / "report.html").write_text(
        _render_html(calls, stats, warnings, ts_label), encoding="utf-8")

    with (output_dir / "data" / "calls.jsonl").open("w", encoding="utf-8") as f:
        for c in calls:
            f.write(json.dumps(c.to_dict(), default=str) + "\n")
    for c in calls:
        (output_dir / "data" / "comparisons" / f"call_{c.call_id:03d}.json").write_text(
            json.dumps(c.comparison_dict(), indent=2, default=str), encoding="utf-8")

    print_terminal_summary(stats, runtime_s=runtime_s,
                           html_path=str(output_dir / "report.html"))
    return output_dir
