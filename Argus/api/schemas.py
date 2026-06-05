# api/schemas.py

from pydantic import BaseModel, Field
from typing import Any, Optional


class TaskRequest(BaseModel):
    id:      Optional[str] = None
    type:    str           = "qa"
    prompt:  str
    urgency: float         = Field(0.5, ge=0.0, le=1.0)


class PreCallResponse(BaseModel):
    action:          str
    model:           Optional[str] = None
    task_id:         Optional[str] = None
    source:          Optional[str] = None
    cached_response: Optional[dict[str, Any]] = None
    tokens_saved:    Optional[int] = None
    reason:          Optional[str] = None
    meta:            Optional[dict[str, Any]] = None


class LLMResult(BaseModel):
    response:     str
    quality:      float = 0.8
    tokens_in:    int   = 0
    tokens_out:   int   = 0
    tokens_total: int   = 0
    cost:         float = 0.0
    latency:      float = 0.0


class PostCallRequest(BaseModel):
    agent_id:   str
    task:       TaskRequest
    decision:   dict[str, Any]
    llm_result: LLMResult


class StateResponse(BaseModel):
    router: dict[str, Any]
    cache:  dict[str, Any]
    dedup:  dict[str, Any]
    budget: dict[str, Any]
    prefix: dict[str, Any]
    cusum:  dict[str, Any]
    events: list[dict[str, Any]]
