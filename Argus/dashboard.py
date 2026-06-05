# dashboard.py
"""Launch: streamlit run dashboard.py"""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import threading
import time
from collections import deque
from core.pipeline    import OptimizationPipeline
from simulation.agent import SimulatedAgent
from core.toon        import toon_savings_report
from eval.report      import generate_visual_report
from eval.metrics     import TestReport
from agents.base_agent import CallResult


def _build_report(pipeline) -> TestReport:
    """Construct a TestReport from the pipeline's logged events so the
    visual report can be generated live from the dashboard."""
    events = pipeline.logger.recent(500)
    results = []
    for e in events:
        model = e.get("model", "")
        if model == "blocked":
            source = "blocked"
        elif model in ("cache", ""):
            source = "semantic_cache"
        else:
            source = "llm"
        results.append(CallResult(
            task_id=e.get("task_id", ""),
            model=model,
            response="",
            tokens_in=0,
            tokens_out=0,
            tokens_total=int(e.get("tokens_total", 0) or 0),
            cost=float(e.get("cost", 0.0) or 0.0),
            latency=0.0,
            quality=float(e.get("quality", 0.0) or 0.0),
            source=source,
        ))
    wasteful = [r for r, e in zip(results, events)
                if e.get("agent_id") == "agent-wasteful"]
    return TestReport(
        pipeline=pipeline,
        all_results=results,
        wasteful_results=wasteful,
        agent_names=["hr", "dev", "finance", "wasteful", "spammer"],
    )

st.set_page_config(page_title="Argus", layout="wide", page_icon="A")
st.title("Argus -- AI Agent Token Governance")

if "pipeline" not in st.session_state:
    st.session_state.pipeline = OptimizationPipeline()
    st.session_state.agents   = []
    st.session_state.running  = False
    st.session_state.history  = deque(maxlen=500)

pipeline = st.session_state.pipeline

with st.sidebar:
    st.header("Control Room")
    if st.button("Start Simulation", type="primary",
                 disabled=st.session_state.running):
        agents = [
            SimulatedAgent("agent-finance-1", "normal",   pipeline, 1.5),
            SimulatedAgent("agent-finance-2", "normal",   pipeline, 1.0),
            SimulatedAgent("agent-hr-1",      "normal",   pipeline, 0.8),
            SimulatedAgent("agent-wasteful",  "wasteful", pipeline, 1.2),
            SimulatedAgent("agent-stuck",     "stuck",    pipeline, 0.5),
        ]
        st.session_state.agents  = agents
        st.session_state.running = True
        for a in agents:
            t = threading.Thread(target=a.run, args=(300,), daemon=True)
            t.start()

    if st.button("Stop"):
        for a in st.session_state.agents:
            a.running = False
        st.session_state.running = False

    st.divider()
    refresh = st.slider("Refresh interval (s)", 1, 10, 2)

    st.divider()
    col_a, col_b = st.columns(2)

    with col_a:
        if st.button("Export Report"):
            generate_visual_report(_build_report(pipeline))
            with open("test_results_visual.png", "rb") as f:
                st.download_button(
                    "Download PNG", f,
                    file_name="argus_report.png",
                    mime="image/png",
                )

    with col_b:
        if st.button("Export TOON"):
            pipeline.logger.export_toon("export.toon")
            with open("export.toon", "r") as f:
                st.download_button(
                    "Download TOON", f,
                    file_name="events.toon",
                    mime="text/plain",
                )

    # Live TOON compression metric
    recent_events = pipeline.logger.recent(100)
    if recent_events:
        toon_info = toon_savings_report(recent_events)
        st.caption(
            f"TOON compression: **{toon_info['savings_pct']}%** fewer tokens than JSON  "
            f"({toon_info['json_approx_tokens']} → {toon_info['toon_approx_tokens']} tokens)"
        )

agg    = pipeline.logger.aggregate()
state  = pipeline.router.get_state()
events = pipeline.logger.recent(200)

total_tokens  = agg.get("total_tokens", 0)
baseline_cost = total_tokens / 1000 * 0.003
actual_cost   = agg.get("total_cost", 0)
savings_pct   = (1 - actual_cost / baseline_cost) * 100 if baseline_cost > 0 else 0

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total calls",    agg.get("total_events", 0))
col2.metric("Actual cost",    f"${actual_cost:.3f}")
col3.metric("Cost saved",     f"{savings_pct:.1f}%",
            delta=f"-${baseline_cost - actual_cost:.3f}")
col4.metric("Cache hit rate", f"{pipeline.cache.hit_rate:.1%}")
col5.metric("Avg quality",    f"{agg.get('avg_quality', 0):.3f}")

st.divider()

left, mid, right = st.columns([2, 2, 1])

with left:
    st.subheader("Model routing distribution")
    routing = state.get("routing_dist", {})
    if routing:
        fig = go.Figure(go.Bar(
            x=list(routing.keys()),
            y=[v * 100 for v in routing.values()],
            marker_color=["#2DD4B4", "#F5A623"],
            text=[f"{v:.1%}" for v in routing.values()],
            textposition="outside",
        ))
        fig.update_layout(
            plot_bgcolor="#0C0C12", paper_bgcolor="#0C0C12",
            font_color="white", yaxis_title="% of calls",
            yaxis_range=[0, 100], height=250, margin=dict(t=20, b=20)
        )
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("LinUCB learning curve")
    if events:
        df = pd.DataFrame(events)
        if "cost" in df.columns:
            df["rolling_cost"] = df["cost"].rolling(20, min_periods=1).mean()
            df["idx"]          = range(len(df))
            fig2 = px.line(df, x="idx", y="rolling_cost",
                           color_discrete_sequence=["#7B68EE"])
            fig2.update_layout(
                plot_bgcolor="#0C0C12", paper_bgcolor="#0C0C12",
                font_color="white", height=200,
                xaxis_title="task #", yaxis_title="rolling avg cost",
                margin=dict(t=20, b=20)
            )
            st.plotly_chart(fig2, use_container_width=True)

with mid:
    st.subheader("Quality vs Cost (Pareto)")
    if events:
        df = pd.DataFrame(events)
        if "quality" in df.columns and "cost" in df.columns:
            fig3 = px.scatter(
                df, x="cost", y="quality",
                color="model",
                color_discrete_map={
                    "claude-haiku-4-5":  "#2DD4B4",
                    "claude-sonnet-4-5": "#F5A623",
                },
                opacity=0.6, height=280,
            )
            fig3.update_layout(
                plot_bgcolor="#0C0C12", paper_bgcolor="#0C0C12",
                font_color="white", margin=dict(t=20, b=20)
            )
            st.plotly_chart(fig3, use_container_width=True)

    st.subheader("LogReg complexity predictions")
    if events:
        df = pd.DataFrame(events)
        if "complexity_score" in df.columns:
            fig4 = px.histogram(
                df, x="complexity_score", nbins=20,
                color_discrete_sequence=["#7B68EE"],
                height=200,
            )
            fig4.update_layout(
                plot_bgcolor="#0C0C12", paper_bgcolor="#0C0C12",
                font_color="white", margin=dict(t=20, b=20)
            )
            st.plotly_chart(fig4, use_container_width=True)

with right:
    st.subheader("Alerts")
    cusum_state = pipeline.get_full_state().get("cusum", {})
    for agent_id, cs in cusum_state.items():
        if cs["alerts"] > 0:
            st.error(f"{agent_id}: {cs['alerts']} anomalies")
        else:
            st.success(f"OK {agent_id}")

    st.divider()
    st.subheader("Cache")
    cs = pipeline.cache.stats()
    st.metric("Hit rate",   f"{cs['hit_rate']:.1%}")
    st.metric("Cache size", cs["size"])
    st.metric("Threshold",  f"{cs['threshold']:.3f}")

    st.divider()
    st.subheader("Dedup")
    ds = pipeline.dedup.stats()
    st.metric("Dedup rate", f"{ds['dedup_rate']:.1%}")
    st.metric("Duplicates", ds["duplicates"])

if st.session_state.running:
    time.sleep(refresh)
    st.rerun()
