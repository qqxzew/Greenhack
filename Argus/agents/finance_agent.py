# agents/finance_agent.py

from agents.base_agent import BaseAgent
from core.pipeline import OptimizationPipeline

FINANCE_PERSONA = """You are a financial analyst assistant at TechCorp.
You analyze financial data, summarize reports, and help with budget decisions.
You are precise with numbers and always note uncertainty in projections."""

FINANCE_TASKS = [
    {
        "type": "analysis",
        "prompt": "Analyze these Q3 results: Revenue $4.2M (+12% YoY), COGS $1.8M (+8%), OpEx $2.1M (+22%), Net Loss $0.3M. Main OpEx driver: marketing headcount +40%."
    },
    {
        "type": "summarize",
        "prompt": "Summarize this board update: Revenue beat forecast by 8%. Gross margin compressed 2pp due to cloud infra costs. Sales cycle shortened from 45 to 31 days. Two enterprise logos signed. Runway extended to 28 months at current burn."
    },
    {
        "type": "analysis",
        "prompt": "Compare two pricing strategies: A) Usage-based at $0.10/API call, B) Subscription at $299/month with 5000 calls included. Our median customer uses 3200 calls/month. Which maximizes revenue at 500 customers?"
    },
    {
        "type": "analysis",
        "prompt": "We're considering hiring 3 additional engineers at $180K fully loaded each. Expected output: ship new enterprise feature in Q1 (projected $800K ARR). IRR analysis needed."
    },
    {
        "type": "summarize",
        "prompt": "Summarize budget variance: Engineering over by $240K (new cloud tools). Marketing under by $120K (delayed campaign). Sales at target. G&A over by $45K (legal fees). Total: $165K over budget."
    },
]


def make_finance_agent(pipeline: OptimizationPipeline) -> BaseAgent:
    return BaseAgent("agent-finance", FINANCE_PERSONA, pipeline)
