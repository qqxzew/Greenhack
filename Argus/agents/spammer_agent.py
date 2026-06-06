# agents/spammer_agent.py

from agents.base_agent import BaseAgent
from core.pipeline import OptimizationPipeline

SPAMMER_TASKS = [
    # Group 1: same question, many rephrasings (tests MinHash dedup + semantic cache)
    {"type": "qa", "id": "sp01", "prompt": "What is the remote work policy?"},
    {"type": "qa", "id": "sp02", "prompt": "Can employees work remotely?"},
    {"type": "qa", "id": "sp03", "prompt": "Is remote work allowed at TechCorp?"},
    {"type": "qa", "id": "sp04", "prompt": "What are the rules for working from home?"},
    {"type": "qa", "id": "sp05", "prompt": "Remote work -- what's the policy?"},
    # Group 2: another repeated topic
    {"type": "qa", "id": "sp06", "prompt": "How do I submit a vacation request?"},
    {"type": "qa", "id": "sp07", "prompt": "What is the process to request time off?"},
    {"type": "qa", "id": "sp08", "prompt": "Steps to apply for paid leave?"},
    {"type": "qa", "id": "sp09", "prompt": "How to request vacation days?"},
    {"type": "qa", "id": "sp10", "prompt": "Procedure for booking annual leave?"},
]

SPAMMER_PERSONA = "You are a helpful HR assistant. Answer concisely."


def make_spammer_agent(pipeline: OptimizationPipeline) -> BaseAgent:
    return BaseAgent("agent-spammer", SPAMMER_PERSONA, pipeline)
