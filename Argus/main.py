# main.py
"""
FastAPI entry point.
Run: uvicorn main:app --reload --port 8000
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from api.routes import router

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


@app.get("/")
def root():
    return {
        "name":  "Argus",
        "docs":  "/docs",
        "state": "/v1/state",
    }
