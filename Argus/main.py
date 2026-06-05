# main.py
"""
FastAPI entry point.
Run: uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from api.routes import router

app = FastAPI(
    title="Argus -- AI Agent Token Governance",
    description="Token governance and optimization system for enterprise AI agents.",
    version="0.1.0",
)

app.include_router(router, prefix="/v1")


@app.get("/")
def root():
    return {
        "name":  "Argus",
        "docs":  "/docs",
        "state": "/v1/state",
    }
