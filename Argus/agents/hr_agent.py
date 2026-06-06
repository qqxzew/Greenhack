# agents/hr_agent.py

from agents.base_agent import BaseAgent
from core.pipeline import OptimizationPipeline

HR_PERSONA = """You are an HR assistant for TechCorp, a 500-person software company.
You handle employee questions about policies, onboarding, benefits, and workplace issues.
You are professional, empathetic, and concise. You always follow company policy."""

HR_TASKS = [
    {"type": "qa",          "prompt": "What is the company policy on remote work for contractors?"},
    {"type": "qa",          "prompt": "How many vacation days do full-time employees get per year?"},
    {"type": "qa",          "prompt": "What is the process for submitting expense reimbursements?"},
    {"type": "summarize",   "prompt": "Summarize the key points of our parental leave policy: 16 weeks fully paid for primary caregiver, 6 weeks for secondary, applicable after 6 months tenure, extendable by 4 weeks unpaid."},
    {"type": "translation", "prompt": "Translate to German: Please submit your timesheet by end of business Friday."},
    {"type": "translation", "prompt": "Translate to French: The performance review cycle begins next Monday."},
    {"type": "qa",          "prompt": "Can an employee work from a different country for more than 30 days?"},
    {"type": "summarize",   "prompt": "Summarize: Employee requested accommodation for back pain. Has doctor note. Needs standing desk and ergonomic chair. IT needs to coordinate. Request submitted 2 weeks ago, no response yet."},
    # Intentional near-duplicate (tests semantic cache)
    {"type": "qa",          "prompt": "How many days of paid vacation are employees entitled to?"},
    {"type": "qa",          "prompt": "What's the vacation entitlement for full-time staff?"},
]


def make_hr_agent(pipeline: OptimizationPipeline) -> BaseAgent:
    return BaseAgent("agent-hr", HR_PERSONA, pipeline)
