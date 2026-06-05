# agents/dev_agent.py

from agents.base_agent import BaseAgent
from core.pipeline import OptimizationPipeline

DEV_PERSONA = """You are a senior software engineer at TechCorp specializing in Python and system design.
You review code, debug issues, and answer architectural questions.
You give precise, actionable feedback. You prioritize security and maintainability."""

DEV_TASKS = [
    {
        "type": "code_review",
        "prompt": "Review this Python function for bugs and security issues:\ndef get_user(user_id):\n    query = f'SELECT * FROM users WHERE id={user_id}'\n    return db.execute(query)"
    },
    {
        "type": "code_review",
        "prompt": "Review this code:\nfor i in range(len(items)):\n    result = expensive_api_call(items[i])\n    results.append(result)"
    },
    {
        "type": "analysis",
        "prompt": "Compare REST vs GraphQL for our internal microservices API. We have 12 services, 3 frontend clients, high read volume, occasional bulk data needs."
    },
    {
        "type": "code_review",
        "prompt": "Security review:\nimport pickle\ndef load_user_data(data: bytes):\n    return pickle.loads(data)"
    },
    {
        "type": "analysis",
        "prompt": "We need to choose between PostgreSQL and MongoDB for storing user activity logs: ~50M records/day, mostly append-only, need fast time-range queries, occasional aggregations."
    },
    {
        "type": "code_review",
        "prompt": "Review: passwords = [row['password'] for row in db.query('SELECT password FROM users')]\nif user_input in passwords:\n    grant_access()"
    },
    {
        "type": "analysis",
        "prompt": "Evaluate whether to use Redis or Memcached as our session store. Team knows Redis. Sessions are small (<4KB). We need pub/sub for real-time features."
    },
]


def make_dev_agent(pipeline: OptimizationPipeline) -> BaseAgent:
    return BaseAgent("agent-dev", DEV_PERSONA, pipeline)
