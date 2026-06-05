# agents/registry.py
"""
Lightweight, import-safe roster of the agents Argus governs.

This module intentionally does NOT import base_agent / anthropic so the API can
expose the roster (and merge it with live pipeline metrics) without needing an
API key or the full agent runtime. The `character` field maps each agent to a
visual archetype in the Argus frontend.
"""

AGENT_ROSTER = [
    {
        "id": "agent-dev",
        "name": "Dev",
        "role": "Senior Software Engineer",
        "kind": "productive",
        "character": "guardian",
        "persona": "Reviews code, debugs issues, and answers architecture questions. Prioritises security and maintainability.",
    },
    {
        "id": "agent-finance",
        "name": "Finance",
        "role": "Financial Analyst",
        "kind": "productive",
        "character": "alchemist",
        "persona": "Analyses financial data, summarises reports, and supports budget decisions. Precise with numbers.",
    },
    {
        "id": "agent-hr",
        "name": "HR",
        "role": "People Operations",
        "kind": "productive",
        "character": "mage",
        "persona": "Handles employee questions on policies, onboarding and benefits. Professional, empathetic, concise.",
    },
    {
        "id": "agent-spammer",
        "name": "Spammer",
        "role": "Repetitive Requester",
        "kind": "abuser",
        "character": "punk",
        "persona": "Fires near-duplicate questions in bursts — stress-tests the dedup + semantic cache layers.",
    },
    {
        "id": "agent-wasteful",
        "name": "Wasteful",
        "role": "Unoptimised Control",
        "kind": "abuser",
        "character": "robot",
        "persona": "Always calls the most expensive model with no caching or routing. Trips the CUSUM anomaly detector.",
    },
]

ROSTER_BY_ID = {a["id"]: a for a in AGENT_ROSTER}
