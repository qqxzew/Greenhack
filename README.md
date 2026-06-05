# Argus — AI Agent Control Room

Team: Taumatawhakatangihangakoauauotamateaturipukakapikimaungahoronukupokaiwhenuakitanatahu

Argus is a control room for a fleet of AI agents. The frontend renders each agent
as a character in a themed room; the **Argus backend** is a token-governance engine
that routes, caches, dedups and watches every LLM call — flagging agents that waste
tokens, loop, or blow their budget.

## Frontend (`index.html`)

A single self-contained file (no build step). Open it directly, or serve it:

```bash
python -m http.server 8765
# then open http://localhost:8765/index.html
```

- **Workspace** — browse agents as 3D rooms; "Nodes" shows the whole hive.
- **Integrations / Analytics** — connect services; live governance charts.
- It runs fully on bundled **demo** data with no backend. When the Argus backend
  is reachable it switches to **live** agents automatically (top-bar chip shows
  `Live · Argus`). Configure the backend URL under **Settings**.

## Backend (`Argus/`)

FastAPI token-governance service. The frontend reads `GET /v1/agents`.

```bash
cd Argus
cp .env.example .env            # optional — only needed for real LLM calls
python -m pip install -r requirements.txt
python -m uvicorn main:app --port 8000

# in another shell — populate live metrics without an API key:
python seed_demo.py
```

Key endpoints (`/v1`): `agents`, `state`, `metrics`, `pre_call`, `post_call`, `health`.
See `Argus/METHODOLOGY.md` for how routing, budgeting, dedup, CUSUM and SPRT work.

> `Argus/.env` (a real API key) is intentionally **not** committed — use `.env.example`.
