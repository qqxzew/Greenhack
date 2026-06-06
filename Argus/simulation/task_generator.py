# simulation/task_generator.py

import random
import numpy as np
import uuid
import time

TASK_TYPES = {
    "summarize":   {"weight": 0.30, "complexity": (0.10, 0.35), "best_model": "haiku"},
    "qa":          {"weight": 0.25, "complexity": (0.05, 0.25), "best_model": "haiku"},
    "translation": {"weight": 0.10, "complexity": (0.10, 0.40), "best_model": "haiku"},
    "code_review": {"weight": 0.20, "complexity": (0.55, 0.90), "best_model": "sonnet"},
    "analysis":    {"weight": 0.15, "complexity": (0.60, 0.95), "best_model": "sonnet"},
}

TEMPLATES = {
    "summarize":   [
        "Summarize the following meeting notes in 3 bullet points: {content}",
        "Give me a concise summary of this document: {content}",
    ],
    "qa":          [
        "What is the company policy on {content}?",
        "Answer this HR question briefly: {content}",
    ],
    "translation": [
        "Translate to German: {content}",
        "Translate the following to French: {content}",
    ],
    "code_review": [
        "Review this Python code for bugs and security issues: {content}",
        "Analyze this function and suggest improvements: {content}",
    ],
    "analysis":    [
        "Analyze the business implications of: {content}",
        "Compare these two strategies and give a recommendation: {content}",
    ],
}

CONTENT = {
    "summarize":   [
        "Q3 revenue up 12%, marketing 30% over budget, product launch delayed 2 weeks.",
        "Team agreed on React migration postponement. API v2 on track for November release.",
    ],
    "qa":          ["remote work policy", "vacation days for contractors", "expense reimbursement"],
    "translation": ["Please submit your timesheet by end of Friday.", "The meeting is at 10am Monday."],
    "code_review": [
        "def get_user(id): return db.query(f'SELECT * FROM users WHERE id={id}')",
        "for i in range(len(items)): process(items[i])",
    ],
    "analysis":    [
        "expanding to Eastern Europe vs doubling down on core markets",
        "switching cloud provider from AWS to Azure",
    ],
}


def generate_task(task_type: str | None = None) -> dict:
    if task_type is None:
        task_type = random.choices(
            list(TASK_TYPES.keys()),
            weights=[v["weight"] for v in TASK_TYPES.values()]
        )[0]

    spec     = TASK_TYPES[task_type]
    content  = random.choice(CONTENT[task_type])
    template = random.choice(TEMPLATES[task_type])
    complexity = float(np.random.uniform(*spec["complexity"]))

    return {
        "id":         str(uuid.uuid4())[:8],
        "type":       task_type,
        "prompt":     template.format(content=content),
        "complexity": complexity,
        "urgency":    float(np.random.beta(2, 5)),
        "best_model": spec["best_model"],
        "timestamp":  time.time(),
    }
