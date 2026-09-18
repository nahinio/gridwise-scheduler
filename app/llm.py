"""Operator-note interpretation: language model first, deterministic guardrails always.

Per note:  primary model -> (one corrective re-ask) -> fallback model -> optional second
vendor -> deterministic reader. Every hop is bounded by a per-call timeout AND by one
request-wide deadline, so interpretation can never push a response past the client's
timeout. No provider failure ever surfaces as an HTTP 5xx.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, Protocol

import openai
import structlog

from app import heuristic
from app.cache import SingleFlightCache
from app.config import PROMPT_VERSION, Settings
from app.guardrails import DEFAULT_EXPLANATIONS, Accepted, GuardrailReject, validate_interpretation
from app.metrics import Metrics
from app.prompts import PROMPT_CACHE_KEY, RESPONSE_FORMAT, build_messages, correction_messages
from app.schemas import Directive, NoOp

log = structlog.get_logger(__name__)

_MIN_CALL_SECONDS = 0.75  # below this there is no point starting another provider call
_VALUE_FIELDS = ("factor", "minimum_energy_kwh", "max_grid_kwh")


class LLMError(Exception):
    """A provider call failed; `kind` is timeout | rate_limit | provider | parse."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(detail or kind)
        self.kind = kind


class ChatProvider(Protocol):
    name: str

    async def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str: ...


@dataclass(frozen=True)
class InterpretResult:
    directive: Directive
    explanation: str
    provider: str  # "openai:<model>" | "gemini:<model>" | "heuristic" | "noop_fallback"
    guardrail: str  # pass_first_try | pass_after_retry | fallback
    attempts: int = 0
    cache_hit: bool = False
    latency_ms: float = 0.0
    raw: dict[str, Any] | None = None  # last model output, exposed only by POST /interpret


class OpenAICompatProvider:
    """One model behind an OpenAI-compatible chat endpoint.

    Optional request parameters differ between providers and model generations, so a
    parameter the endpoint rejects is dropped once and remembered for later calls.
    """

    def __init__(
        self,
        name: str,
        client: openai.AsyncOpenAI,
        model: str,
        metrics: Metrics,
        *,
        max_output_tokens: int,
        reasoning_effort: str | None = None,
        prompt_cache_key: str | None = None,
        temperature: float | None = None,
    ) -> None:
        self.name = name
        self._client = client
        self._model = model
        self._metrics = metrics
        self._params: dict[str, Any] = {
            "response_format": RESPONSE_FORMAT,
            "max_completion_tokens": max_output_tokens,
        }
        if reasoning_effort:
            self._params["reasoning_effort"] = reasoning_effort
        if prompt_cache_key:
            self._params["prompt_cache_key"] = prompt_cache_key
        if temperature is not None:
            self._params["temperature"] = temperature

    def _adapt(self, error_text: str) -> bool:
        """Drop / downgrade the parameter the endpoint complained about. True if changed."""
        text = error_text.lower()
        for name in ("reasoning_effort", "prompt_cache_key", "temperature"):
            if name in text and name in self._params:
                del self._params[name]
                return True
        if "max_completion_tokens" in text and "max_completion_tokens" in self._params:
            self._params["max_tokens"] = self._params.pop("max_completion_tokens")
            return True
        if ("json_schema" in text or "response_format" in text) and self._params[
            "response_format"
        ] is RESPONSE_FORMAT:
            self._params["response_format"] = {"type": "json_object"}
            return True
        return False

    async def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        for _ in range(5):
            try:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=messages,  # type: ignore[arg-type]
                    timeout=timeout,
                    **self._params,
                )
            except openai.BadRequestError as error:
                if self._adapt(str(error)):
                    log.warning("llm_param_dropped", provider=self.name, params=list(self._params))
                    continue
                raise LLMError("provider", "bad request") from error
            except openai.APITimeoutError as error:
                raise LLMError("timeout") from error
            except openai.RateLimitError as error:
                raise LLMError("rate_limit") from error
            except openai.OpenAIError as error:
                raise LLMError("provider", type(error).__name__) from error

            usage = getattr(response, "usage", None)
            if usage is not None:
                self._metrics.inc("tokens_in", amount=getattr(usage, "prompt_tokens", 0) or 0)
                self._metrics.inc("tokens_out", amount=getattr(usage, "completion_tokens", 0) or 0)
            content = response.choices[0].message.content if response.choices else None
            if not content:
                raise LLMError("parse", "empty completion")
            return content
        raise LLMError("provider", "parameter negotiation did not converge")


def build_providers(settings: Settings, metrics: Metrics) -> list[ChatProvider]:
    """Provider chain in priority order; empty means degraded (deterministic-only) mode."""
    providers: list[ChatProvider] = []
    if settings.openai_api_key is not None:
        client = openai.AsyncOpenAI(
            api_key=settings.openai_api_key.get_secret_value(),
            base_url=settings.openai_base_url,
            max_retries=0,  # retries are ours: they must respect the request deadline
        )
        for model in dict.fromkeys([settings.openai_model, settings.openai_fallback_model]):
            providers.append(
                OpenAICompatProvider(
                    f"openai:{model}",
                    client,
                    model,
                    metrics,
                    max_output_tokens=settings.llm_max_output_tokens,
                    reasoning_effort=settings.openai_reasoning_effort,
                    prompt_cache_key=PROMPT_CACHE_KEY,
                )
            )
    if settings.gemini_api_key is not None:
        providers.append(
            OpenAICompatProvider(
                f"gemini:{settings.gemini_model}",
                openai.AsyncOpenAI(
                    api_key=settings.gemini_api_key.get_secret_value(),
                    base_url=settings.gemini_base_url,
                    max_retries=0,
                ),
                settings.gemini_model,
                metrics,
                max_output_tokens=settings.llm_max_output_tokens,
                temperature=0,
            )
        )
    return providers


def parse_model_json(text: str) -> dict[str, Any]:
    """Model text -> dict. Tolerates a ```json fence; anything else is a parse error."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").removeprefix("json").strip()
    try:
        parsed = json.loads(cleaned)
    except ValueError as error:
        raise LLMError("parse", "completion is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise LLMError("parse", "completion is not a JSON object")
    return parsed


def crosscheck_hint(note: str, capacity_kwh: float, directive: Directive) -> str | None:
    """Neutral re-ask hint when the deterministic reader confidently disagrees, else None."""
    reading = heuristic.interpret(note, capacity_kwh)
    parsed = reading.directive
    if isinstance(parsed, NoOp):
        return None
    if isinstance(directive, NoOp):
        if reading.hours_confident and reading.value_confident:
            return (
                f"A deterministic parser found an explicit {parsed.directive_type} limit for "
                f"hours {list(parsed.hours)} in this note, but you answered no_op."
            )
        return None
    if parsed.directive_type != directive.directive_type:
        return None
    if reading.hours_confident and parsed.hours != directive.hours:
        return (
            f"A deterministic parser read the time window as hours {list(parsed.hours)} "
            f"(start hour included, end hour excluded); you answered {list(directive.hours)}."
        )
    if reading.value_confident:
        for field in _VALUE_FIELDS:
            ours, theirs = getattr(parsed, field, None), getattr(directive, field, None)
            if ours is not None and theirs is not None and abs(ours - theirs) > 0.01:
                return f"A deterministic parser read {field} as {ours:g}; you answered {theirs:g}."
    return None


class NoteInterpreter:
    def __init__(self, settings: Settings, providers: list[ChatProvider], metrics: Metrics) -> None:
        self._settings = settings
        self._providers = providers
        self._metrics = metrics
        self._cache: SingleFlightCache[InterpretResult] = SingleFlightCache(
            settings.cache_max_entries
        )
        self._slots = asyncio.Semaphore(max(1, settings.llm_max_concurrency))

    @property
    def degraded(self) -> bool:
        return not self._providers

    async def interpret_all(self, notes: list[str], capacity_kwh: float) -> list[InterpretResult]:
        """One result per note, in note order. Never raises."""
        deadline = time.monotonic() + self._settings.llm_total_budget_s
        return list(
            await asyncio.gather(
                *(self.interpret_note(n, i, capacity_kwh, deadline) for i, n in enumerate(notes))
            )
        )

    async def interpret_note(
        self, note: str, note_index: int, capacity_kwh: float, deadline: float | None = None
    ) -> InterpretResult:
        started = time.monotonic()
        if deadline is None:
            deadline = started + self._settings.llm_total_budget_s
        text = note.strip()[: self._settings.max_note_chars]
        self._metrics.inc("notes_total")
        key = hashlib.sha256(
            f"{PROMPT_VERSION}|{capacity_kwh!r}|{' '.join(text.lower().split())}".encode()
        ).hexdigest()

        async def compute() -> tuple[InterpretResult, bool]:
            try:
                result = await self._run_chain(text, note_index, capacity_kwh, deadline)
            except Exception:  # defensive: interpretation must never take the request down
                log.exception("interpretation_failed")
                result = InterpretResult(
                    NoOp(),
                    "Could not interpret the note confidently; treated as not applicable.",
                    "noop_fallback",
                    "fallback",
                )
            self._metrics.inc("provider_used", result.provider)
            return result, result.guardrail != "fallback"

        result, shared = await self._cache.get_or_compute(key, compute)
        if shared:
            self._metrics.inc("cache_hits")
        elapsed = (time.monotonic() - started) * 1000
        self._metrics.observe("llm_ms", elapsed)
        return replace(result, cache_hit=shared, latency_ms=round(elapsed, 1))

    async def _ask(
        self, provider: ChatProvider, messages: list[dict[str, str]], deadline: float
    ) -> str:
        async with self._slots:
            budget = min(self._settings.llm_timeout_s, deadline - time.monotonic())
            if budget < _MIN_CALL_SECONDS:
                raise LLMError("timeout", "request deadline reached")
            try:
                return await asyncio.wait_for(
                    provider.complete(messages, timeout=budget), timeout=budget
                )
            except TimeoutError as error:
                raise LLMError("timeout") from error

    async def _run_chain(
        self, note: str, note_index: int, capacity_kwh: float, deadline: float
    ) -> InterpretResult:
        base = build_messages(note, note_index, capacity_kwh)
        attempts = 0
        crosschecked = not self._settings.crosscheck_enabled

        for position, provider in enumerate(self._providers):
            follow_up: list[dict[str, str]] = []
            for _ in range(2 if position == 0 else 1):  # only the primary gets a re-ask
                attempts += 1
                try:
                    text = await self._ask(provider, base + follow_up, deadline)
                    raw = parse_model_json(text)
                except LLMError as error:
                    self._metrics.inc("llm_errors", error.kind)
                    log.warning("llm_call_failed", provider=provider.name, kind=error.kind)
                    if error.kind == "parse":
                        follow_up = correction_messages(
                            None, "invalid_json", "Return one valid JSON object only."
                        )
                    continue

                verdict = validate_interpretation(
                    raw, note_index=note_index, capacity_kwh=capacity_kwh
                )
                if isinstance(verdict, GuardrailReject):
                    self._metrics.inc("guardrail_rejects", verdict.reason)
                    log.warning("guardrail_reject", provider=provider.name, reason=verdict.reason)
                    follow_up = correction_messages(text, verdict.reason, verdict.fix)
                    continue

                if not crosschecked:
                    crosschecked = True
                    hint = crosscheck_hint(note, capacity_kwh, verdict.directive)
                    if hint:
                        verdict, raw, attempts = await self._second_opinion(
                            provider,
                            base,
                            text,
                            hint,
                            verdict,
                            raw,
                            attempts,
                            note_index,
                            capacity_kwh,
                            deadline,
                        )
                return InterpretResult(
                    verdict.directive,
                    verdict.explanation,
                    provider.name,
                    "pass_first_try" if attempts == 1 else "pass_after_retry",
                    attempts,
                    raw=raw,
                )

        return self._deterministic(note, capacity_kwh, attempts)

    async def _second_opinion(
        self,
        provider: ChatProvider,
        base: list[dict[str, str]],
        first_text: str,
        hint: str,
        first: Accepted,
        first_raw: dict[str, Any],
        attempts: int,
        note_index: int,
        capacity_kwh: float,
        deadline: float,
    ) -> tuple[Accepted, dict[str, Any], int]:
        """Cross-check disagreement: ask the model to re-read once. Its valid answer stands."""
        self._metrics.inc("crosscheck_reasks")
        fix = "Correct the JSON if the parser is right; repeat your answer if you are certain."
        try:
            text = await self._ask(
                provider, base + correction_messages(first_text, hint, fix), deadline
            )
            raw = parse_model_json(text)
        except LLMError as error:
            self._metrics.inc("llm_errors", error.kind)
            return first, first_raw, attempts + 1
        second = validate_interpretation(raw, note_index=note_index, capacity_kwh=capacity_kwh)
        if isinstance(second, GuardrailReject):
            self._metrics.inc("guardrail_rejects", second.reason)
            return first, first_raw, attempts + 1
        if second.directive != first.directive:
            self._metrics.inc("crosscheck_changed")
        return second, raw, attempts + 1

    def _deterministic(self, note: str, capacity_kwh: float, attempts: int) -> InterpretResult:
        """Last hop: explicit-wording reader, itself bounded by the same directive models."""
        if self._providers:
            log.error("all_llm_hops_failed", attempts=attempts)
        reading = heuristic.interpret(note, capacity_kwh)
        explanation = reading.explanation or DEFAULT_EXPLANATIONS[reading.directive.directive_type]
        return InterpretResult(reading.directive, explanation, "heuristic", "fallback", attempts)

    async def warm_up(self) -> None:
        """Prime provider connections and the prompt cache. Best effort, result discarded."""
        if self._providers:
            await self.interpret_note("The cafeteria menu changes tomorrow.", 0, 100.0)
