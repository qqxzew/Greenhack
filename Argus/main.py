# main.py
"""
FastAPI entry point.
Run: uvicorn main:app --reload --port 8000
"""

import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from api.routes import router

_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_FRONTEND = os.path.join(_ROOT, "index.html")

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


# Serve static assets referenced by index.html (images, videos, logo)
for _dir in ("videos", "background images", "OFFICE ALL CHARAKTER"):
    _path = os.path.join(_ROOT, _dir)
    if os.path.isdir(_path):
        app.mount("/" + _dir, StaticFiles(directory=_path), name=_dir)

_logo = os.path.join(_ROOT, "argus-logo.png")
if os.path.isfile(_logo):
    @app.get("/argus-logo.png", include_in_schema=False)
    def logo():
        return FileResponse(_logo, media_type="image/png")
