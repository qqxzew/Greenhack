# core/toon.py

import json
import time

FIELDS = ["ts", "agt", "mdl", "typ", "q", "usd", "tok", "cx", "anom", "sprt"]

MODEL_ENC = {
    "claude-haiku-4-5":  "h",
    "claude-sonnet-4-5": "s",
    "cache":             "c",
    "blocked":           "b",
}
MODEL_DEC = {v: k for k, v in MODEL_ENC.items()}

AGENT_ENC = {
    "agent-hr":       "hr",
    "agent-dev":      "dv",
    "agent-finance":  "fn",
    "agent-wasteful": "wx",
    "agent-spammer":  "sp",
}
AGENT_DEC = {v: k for k, v in AGENT_ENC.items()}

TYPE_ENC = {
    "summarize":   "sm",
    "qa":          "qa",
    "code_review": "cd",
    "analysis":    "an",
    "translation": "tr",
    "generation":  "gn",
}
TYPE_DEC = {v: k for k, v in TYPE_ENC.items()}

HEADER = (
    "#TOON/1.0\n"
    "@S:" + "|".join(FIELDS) + "\n"
    "@M:" + ",".join(f"{v}={k}" for k, v in MODEL_ENC.items()) + "\n"
    "@A:" + ",".join(f"{v}={k}" for k, v in AGENT_ENC.items()) + "\n"
    "@T:" + ",".join(f"{v}={k}" for k, v in TYPE_ENC.items()) + "\n"
)


def _enc_float(v: float) -> str:
    """
    Encode float compactly.
    0.871  → '.871'
    1.0    → '1'
    0.0    → '0'
    0.0001 → '.0001'
    """
    if v == 0.0:
        return "0"
    s = f"{v:.6f}".rstrip("0").rstrip(".")
    if s.startswith("0."):
        s = s[1:]        # '.871' not '0.871'
    elif s.startswith("-0."):
        s = "-" + s[2:]  # '-.5' not '-0.5'
    return s


def _dec_float(s: str) -> float:
    """Decode compact float back to Python float."""
    if not s or s == "0":
        return 0.0
    if s.startswith("."):
        s = "0" + s
    elif s.startswith("-."):
        s = "-0" + s[1:]
    return float(s)


def encode_event(event: dict) -> str:
    """Encode one event dict to a single TOON record line."""
    sprt_raw = str(event.get("sprt", "CONTINUE"))
    sprt_map = {"CONTINUE": "C", "STOP_PROGRESSING": "P", "STOP_STUCK": "S"}
    sprt_char = sprt_map.get(sprt_raw, sprt_raw[0] if sprt_raw else "C")

    parts = [
        str(int(event.get("ts", int(time.time())))),
        AGENT_ENC.get(event.get("agent_id", ""), event.get("agent_id", "?")[:4]),
        MODEL_ENC.get(event.get("model", ""),    event.get("model", "?")[:2]),
        TYPE_ENC.get(event.get("task_type", ""), event.get("task_type", "?")[:2]),
        _enc_float(float(event.get("quality",          0.0))),
        _enc_float(float(event.get("cost",              0.0))),
        str(int(event.get("tokens_total",               0))),
        _enc_float(float(event.get("complexity_score",  0.0))),
        "1" if event.get("is_anomaly") else "0",
        sprt_char,
    ]
    return "|".join(parts)


def decode_event(line: str) -> dict:
    """Decode one TOON record line to an event dict."""
    parts = line.strip().split("|")
    if len(parts) < len(FIELDS):
        return {}
    sprt_map = {"C": "CONTINUE", "P": "STOP_PROGRESSING", "S": "STOP_STUCK"}
    return {
        "ts":               int(parts[0]),
        "agent_id":         AGENT_DEC.get(parts[1], parts[1]),
        "model":            MODEL_DEC.get(parts[2], parts[2]),
        "task_type":        TYPE_DEC.get(parts[3], parts[3]),
        "quality":          _dec_float(parts[4]),
        "cost":             _dec_float(parts[5]),
        "tokens_total":     int(parts[6]),
        "complexity_score": _dec_float(parts[7]),
        "is_anomaly":       parts[8] == "1",
        "sprt":             sprt_map.get(parts[9], parts[9]),
    }


def encode_stream(events: list[dict]) -> str:
    """Encode a list of event dicts to a full TOON document string."""
    lines = [HEADER.rstrip("\n")]
    for e in events:
        if e:
            lines.append(encode_event(e))
    return "\n".join(lines)


def decode_stream(toon_str: str) -> list[dict]:
    """Decode a TOON document string to a list of event dicts."""
    events = []
    for line in toon_str.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("@"):
            continue
        ev = decode_event(line)
        if ev:
            events.append(ev)
    return events


def toon_savings_report(events: list[dict]) -> dict:
    """Compare TOON size vs JSON size for the same event list."""
    json_str = json.dumps(events)
    toon_str = encode_stream(events)
    return {
        "json_chars":          len(json_str),
        "toon_chars":          len(toon_str),
        "savings_pct":         round((1 - len(toon_str) / len(json_str)) * 100, 1),
        "json_approx_tokens":  len(json_str) // 4,
        "toon_approx_tokens":  len(toon_str) // 4,
    }
