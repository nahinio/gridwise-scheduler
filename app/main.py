"""Application factory: wiring, middleware, exception handlers, OpenAPI."""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException

from app import errors
from app.config import Settings, get_settings
from app.llm import NoteInterpreter, build_providers
from app.logs import configure_logging
from app.metrics import Metrics
from app.routers import core, optional
from app.schemas import OptimizeRequest, SemanticError

log = structlog.get_logger(__name__)

_DESCRIPTION = """\
LLM-assisted 24-hour campus energy scheduler (BUP CSE Fest 2026 - GridWise).

**Pipeline:** operator notes -> LLM interpreter -> deterministic guardrails -> directive merge
-> exact LP optimizer (HiGHS) -> judge-clone replay validator -> response.
"""
_TAGS = [
    {"name": "Required - judged", "description": "The contract the judge harness exercises."},
    {"name": "Optional - not judged", "description": "Observability and debugging helpers."},
]


def create_app(
    settings: Settings | None = None, interpreter: NoteInterpreter | None = None
) -> FastAPI:
    settings = settings or get_settings()
    secrets = [
        key.get_secret_value()
        for key in (settings.openai_api_key, settings.gemini_api_key)
        if key is not None
    ]
    configure_logging(settings.log_level, secrets)
    metrics = Metrics()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        providers = settings.configured_providers()
        if providers:
            log.info("startup", providers=providers)
        else:
            log.warning("startup_degraded_mode", reason="no LLM key: deterministic reader only")
        # Warm-up never blocks readiness: /health is green while this runs (or fails).
        warm_up = asyncio.create_task(app.state.interpreter.warm_up())
        yield
        warm_up.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await warm_up

    app = FastAPI(
        title="GridWise Scheduler",
        version="1.0.0",
        description=_DESCRIPTION,
        openapi_tags=_TAGS,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.metrics = metrics
    app.state.interpreter = interpreter or NoteInterpreter(
        settings, build_providers(settings, metrics), metrics
    )

    @app.middleware("http")
    async def request_context(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        started = time.monotonic()
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if 0 < len(incoming) <= 64 and incoming.isprintable() else None
        request.state.request_id = request_id or uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request.state.request_id, path=request.url.path
        )
        try:
            response = await call_next(request)
        except Exception:
            log.exception("unhandled_error")  # traceback goes to logs only, never to the client
            response = errors.envelope(request, 500, "unexpected internal error")
        elapsed_ms = (time.monotonic() - started) * 1000
        metrics.inc("requests_total")
        metrics.inc("requests_by_status", str(response.status_code))
        if request.url.path == "/optimize-energy":
            metrics.observe("total_ms", elapsed_ms)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    app.add_exception_handler(errors.BadRequest, errors.bad_request_handler)
    app.add_exception_handler(RequestValidationError, errors.request_validation_handler)
    app.add_exception_handler(SemanticError, errors.semantic_handler)
    app.add_exception_handler(HTTPException, errors.http_exception_handler)

    app.include_router(core.router)
    if settings.enable_optional_endpoints:
        app.include_router(optional.router)

    _document_request_body(app)
    return app


def _document_request_body(app: FastAPI) -> None:
    """The judged route parses its body by hand (any Content-Type, exact 400 mapping), so
    its request schema is added to the OpenAPI document explicitly."""
    generate = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema
        document = generate()  # also stores the document on app.openapi_schema
        schema = OptimizeRequest.model_json_schema(ref_template="#/components/schemas/{model}")
        components = document.setdefault("components", {}).setdefault("schemas", {})
        components.update(schema.pop("$defs", {}))
        components["OptimizeRequest"] = schema
        return document

    app.openapi = openapi  # type: ignore[method-assign]


app = create_app()
