# api/routes.py

from fastapi import APIRouter, HTTPException
from api.schemas import (
    TaskRequest, PreCallResponse, PostCallRequest, StateResponse
)
from core.pipeline import OptimizationPipeline


_pipeline: OptimizationPipeline | None = None


def get_pipeline() -> OptimizationPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = OptimizationPipeline()
    return _pipeline


router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.post("/pre_call", response_model=PreCallResponse)
def pre_call(agent_id: str, task: TaskRequest):
    pipeline = get_pipeline()
    decision = pipeline.pre_call(agent_id, task.model_dump())
    return decision


@router.post("/post_call")
def post_call(req: PostCallRequest):
    pipeline = get_pipeline()
    event = pipeline.post_call(
        req.agent_id,
        req.task.model_dump(),
        req.decision,
        req.llm_result.model_dump(),
    )
    return event


@router.get("/state", response_model=StateResponse)
def state():
    pipeline = get_pipeline()
    return pipeline.get_full_state()


@router.get("/metrics")
def metrics():
    pipeline = get_pipeline()
    return pipeline.logger.aggregate()
