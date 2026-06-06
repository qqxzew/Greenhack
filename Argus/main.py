# main.py
"""
FastAPI entry point.
Run: uvicorn main:app --reload --port 8000
"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from api.routes import router

_FRONTEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")

app = FastAPI(
    title="Argus -- AI Agent Token Governance",
    description="Token governance and optimization system for enterprise AI agents.",
    version="0.1.0",
)

# Allow the Argus frontend (served from any local origin / file host) to call
# the API from the browser. Tighten allow_origins for production.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/v1")


@app.get("/", include_in_schema=False)
def root():
    if os.path.isfile(_FRONTEND):
        return FileResponse(_FRONTEND, media_type="text/html")
    return {
        "name":  "Argus",
        "docs":  "/docs",
        "state": "/v1/state",
    }
