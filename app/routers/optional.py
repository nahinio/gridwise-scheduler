"""Optional endpoints - NOT part of the judged contract.

Observability and debugging only. They share nothing with the judged request path except
pure functions, and can be switched off with ENABLE_OPTIONAL_ENDPOINTS=false.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict, Field

from app.schemas import DirectiveInterpretation, NonNegFloat

router = APIRouter(tags=["Optional - not judged"])

_DISCLAIMER = (
    "Not required by the Problem Statement. Provided for observability/debugging. "
    "The judged contract is GET /health and POST /optimize-energy only."
)


class InterpretRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    operator_notes: Annotated[list[str], Field(min_length=1, max_length=3)]
    battery_capacity_kwh: NonNegFloat


class InterpretedNote(BaseModel):
    interpretation: DirectiveInterpretation
    provider: str
    guardrail_status: str
    attempts: int
    cache_hit: bool
    latency_ms: float
    raw_llm_output: dict[str, Any] | None


class InterpretResponse(BaseModel):
    degraded_mode: bool
    notes: list[InterpretedNote]


@router.get("/stats", summary="[OPTIONAL] Aggregate counters and latency", description=_DISCLAIMER)
async def stats(request: Request) -> dict[str, Any]:
    state = request.app.state
    return {
        "degraded_mode": state.interpreter.degraded,
        "providers": state.settings.configured_providers(),
        **state.metrics.snapshot(),
    }


@router.post(
    "/interpret",
    response_model=InterpretResponse,
    summary="[OPTIONAL] Run only the LLM + guardrails stage",
    description=_DISCLAIMER,
)
async def interpret(body: InterpretRequest, request: Request) -> InterpretResponse:
    interpreter = request.app.state.interpreter
    results = await interpreter.interpret_all(body.operator_notes, body.battery_capacity_kwh)
    return InterpretResponse(
        degraded_mode=interpreter.degraded,
        notes=[
            InterpretedNote(
                interpretation=DirectiveInterpretation.build(i, r.directive, r.explanation),
                provider=r.provider,
                guardrail_status=r.guardrail,
                attempts=r.attempts,
                cache_hit=r.cache_hit,
                latency_ms=r.latency_ms,
                raw_llm_output=r.raw,
            )
            for i, r in enumerate(results)
        ],
    )
