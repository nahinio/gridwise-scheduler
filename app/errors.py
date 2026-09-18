"""Error envelope and status mapping (Problem Statement 6.1).

400 malformed / structurally invalid - 422 well-formed but contradictory - 500 controlled.
Bodies never contain exception class names, file paths, stack frames, or secrets.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from starlette.exceptions import HTTPException

from app.schemas import SemanticError

_NAMES = {400: "bad_request", 404: "not_found", 405: "method_not_allowed", 422: "unprocessable"}


class BadRequest(Exception):
    """Malformed JSON or a structurally invalid request (HTTP 400)."""

    def __init__(self, detail: Any) -> None:
        super().__init__("bad request")
        self.detail = detail


def envelope(request: Request, status: int, detail: Any) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={
            "error": _NAMES.get(status, "internal_error" if status >= 500 else "error"),
            "detail": detail,
            "request_id": getattr(request.state, "request_id", None),
        },
    )


def validation_detail(error: ValidationError | RequestValidationError) -> list[dict[str, str]]:
    """Field path + message only: no echoed input values, no library URLs."""
    return [
        {
            "field": ".".join(str(part) for part in item["loc"] if part != "body") or "body",
            "message": str(item["msg"]).removeprefix("Value error, "),
        }
        for item in error.errors()[:20]
    ]


async def bad_request_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, BadRequest)
    return envelope(request, 400, error.detail)


async def request_validation_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    return envelope(request, 400, validation_detail(error))


async def semantic_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, SemanticError)
    return envelope(request, 422, str(error))


async def http_exception_handler(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, HTTPException)
    return envelope(request, error.status_code, str(error.detail))
