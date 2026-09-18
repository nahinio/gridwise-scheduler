"""The judged contract: GET /health and POST /optimize-energy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import structlog
from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.errors import BadRequest, validation_detail
from app.llm import NoteInterpreter
from app.merge import merge
from app.metrics import Metrics
from app.optimizer import SCHEDULE_BUDGET_S, ScheduleOutcome, idle_plan, plan_schedule
from app.schemas import (
    DirectiveInterpretation,
    HealthResponse,
    OptimizeRequest,
    OptimizeResponse,
    check_semantics,
)
from app.summary import build_summary

log = structlog.get_logger(__name__)
router = APIRouter(tags=["Required - judged"])

# Every LP solve runs on ONE long-lived thread. The native solver stack (HiGHS / BLAS) was
# measured to segfault or deadlock when entered from short-lived or freshly spawned threads,
# which is exactly what a default thread pool does under a burst of requests. A solve takes
# ~5 ms, so serialising them costs nothing and keeps the event loop free.
_SOLVER_THREAD = ThreadPoolExecutor(max_workers=1, thread_name_prefix="lp-solver")

_REQUEST_BODY = {
    "required": True,
    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/OptimizeRequest"}}},
}
_ERRORS: dict[int | str, dict[str, Any]] = {
    400: {"description": "Malformed JSON or structurally invalid request."},
    422: {"description": "Well-formed request with contradictory battery values."},
    500: {"description": "Controlled internal error (no stack trace, no secrets)."},
}


async def read_json_object(request: Request, limit_bytes: int) -> dict[str, Any]:
    """Body -> dict, whatever the Content-Type says. Anything else is a 400."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit_bytes:
        raise BadRequest("request body is too large")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > limit_bytes:
            raise BadRequest("request body is too large")
    try:
        parsed = json.loads(bytes(body))
    except (ValueError, RecursionError):
        raise BadRequest("request body is not valid JSON") from None
    if not isinstance(parsed, dict):
        raise BadRequest("request body must be a JSON object")
    return parsed


@router.get("/health", response_model=HealthResponse, summary="Readiness probe")
async def health() -> HealthResponse:
    """Touches no dependency: green as soon as the process accepts connections."""
    return HealthResponse()


@router.post(
    "/optimize-energy",
    response_model=OptimizeResponse,
    summary="Interpret operator notes and return the cheapest valid 24-hour plan",
    openapi_extra={"requestBody": _REQUEST_BODY},
    responses=_ERRORS,
)
async def optimize_energy(request: Request) -> OptimizeResponse:
    started = time.monotonic()
    state = request.app.state
    interpreter: NoteInterpreter = state.interpreter
    metrics: Metrics = state.metrics

    payload = await read_json_object(request, state.settings.max_body_bytes)
    try:
        scenario = OptimizeRequest.model_validate(payload)
    except ValidationError as error:
        raise BadRequest(validation_detail(error)) from None
    check_semantics(scenario)

    # 1. LLM interpretation, already passed through the deterministic guardrails.
    interpreted = await interpreter.interpret_all(
        scenario.operator_notes, scenario.battery.capacity_kwh
    )
    directives = [item.directive for item in interpreted]
    llm_ms = (time.monotonic() - started) * 1000

    # 2. Exact LP + replay validation, off the event loop, on the dedicated solver thread.
    lp_started = time.monotonic()
    try:
        outcome = await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(
                _SOLVER_THREAD, plan_schedule, scenario, directives
            ),
            SCHEDULE_BUDGET_S + 2,
        )
    except TimeoutError:  # never expected; answer with the always-available idle plan
        log.error("schedule_stage_timeout")
        metrics.inc("schedule_fallback", "stage_timeout")
        limits = merge(scenario, directives)
        outcome = ScheduleOutcome(idle_plan(scenario, limits), directives, [], "idle")
    metrics.observe("lp_ms", (time.monotonic() - lp_started) * 1000)

    if outcome.fallback:
        metrics.inc("schedule_fallback", outcome.fallback)
        log.error(
            "schedule_fallback",
            kind=outcome.fallback,
            relaxed_notes=outcome.relaxed_note_indexes,
            violations=[f"{v.rule}@{v.hour}" for v in outcome.violations][:10],
        )

    log.info(
        "optimized",
        scenario_sha=hashlib.sha256(scenario.scenario_id.encode()).hexdigest()[:12],
        notes=[
            {
                "sha": hashlib.sha256(note.encode()).hexdigest()[:12],
                "chars": len(note),
                "type": item.directive.directive_type,
                "provider": item.provider,
                "guardrail": item.guardrail,
                "cache_hit": item.cache_hit,
            }
            for note, item in zip(scenario.operator_notes, interpreted, strict=True)
        ],
        fallback=outcome.fallback,
        llm_ms=round(llm_ms, 1),
        total_ms=round((time.monotonic() - started) * 1000, 1),
    )

    result = outcome.result
    return OptimizeResponse(
        scenario_id=scenario.scenario_id,
        directive_interpretation=[
            DirectiveInterpretation.build(index, item.directive, item.explanation)
            for index, item in enumerate(interpreted)
        ],
        hourly_plan=result.plan,
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        plan_summary=build_summary(directives, outcome),
    )
