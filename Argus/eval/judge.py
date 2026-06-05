# eval/judge.py

import anthropic
import json


_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


JUDGE_SYSTEM = """You are a quality evaluator. Score the response on a scale 0.0-1.0.
Respond ONLY with JSON: {"score": 0.0, "reason": "str"}
0.9-1.0 = excellent  |  0.7-0.9 = good  |  0.5-0.7 = acceptable  |  below 0.5 = poor"""


def score_quality(task: dict, response: str) -> float:
    """
    Uses claude-haiku-4-5 to score the quality of a response.
    Returns a float in [0, 1].

    This is the reward signal for LinUCB and the label for LogReg.
    """
    if not response or response.strip() in ("{}", ""):
        return 0.1

    prompt = (
        f"Task type: {task.get('type', 'unknown')}\n"
        f"Task: {task.get('prompt', '')[:300]}\n"
        f"Response: {response[:500]}\n\n"
        f"Score this response. Be strict about completeness and relevance."
    )

    try:
        resp = _get_client().messages.create(
            model="claude-haiku-4-5",
            max_tokens=64,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
        data = json.loads(text)
        return float(max(0.0, min(1.0, data.get("score", 0.5))))
    except Exception:
        # Fallback: heuristic quality from JSON validity
        try:
            json.loads(response)
            return 0.75
        except Exception:
            return 0.4
