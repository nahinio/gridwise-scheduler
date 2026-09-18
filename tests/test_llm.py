"""Provider chain behaviour with scripted fake providers - no network, no keys."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import openai
import pytest

import app.llm as llm_module
from app.config import Settings
from app.llm import (
    LLMError,
    NoteInterpreter,
    OpenAICompatProvider,
    crosscheck_hint,
    parse_model_json,
)
from app.metrics import Metrics
from app.schemas import NoCharge, NoOp, SolarReduction

NOTE = "Solar output will drop to about 20% from 1 PM to 3 PM."


def answer(directive_type: str = "solar_reduction", **fields: Any) -> str:
    base = {
        "note_index": 0,
        "applies": directive_type != "no_op",
        "directive_type": directive_type,
        "hours": [13, 14],
        "factor": 0.2,
        "minimum_energy_kwh": None,
        "max_grid_kwh": None,
        "explanation": "test",
    }
    return json.dumps(base | fields)


NO_OP = answer("no_op", hours=[], factor=None)


class FakeProvider:
    """Replays a script of completions / exceptions / delays and records what it was sent."""

    def __init__(self, name: str, script: list[Any]) -> None:
        self.name = name
        self.script = list(script)
        self.calls: list[list[dict[str, str]]] = []

    async def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        self.calls.append(messages)
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, (int, float)):
            await asyncio.sleep(step)
            return answer()
        if isinstance(step, Exception):
            raise step
        return str(step)


def interpreter(*providers: FakeProvider, **overrides: Any) -> tuple[NoteInterpreter, Metrics]:
    metrics = Metrics()
    settings = Settings(_env_file=None, **overrides)  # type: ignore[call-arg]
    return NoteInterpreter(settings, list(providers), metrics), metrics


async def test_first_valid_answer_is_used() -> None:
    primary = FakeProvider("primary", [answer()])
    interp, metrics = interpreter(primary)
    result = await interp.interpret_note(NOTE, 0, 500)
    assert result.directive == SolarReduction(hours=(13, 14), factor=0.2)
    assert (result.provider, result.guardrail, result.attempts) == ("primary", "pass_first_try", 1)
    assert metrics.count("provider_used", "primary") == 1


async def test_provider_errors_fall_through_the_chain() -> None:
    primary = FakeProvider("primary", [LLMError("timeout")])
    backup = FakeProvider("backup", [answer()])
    interp, metrics = interpreter(primary, backup)
    result = await interp.interpret_note(NOTE, 0, 500)
    assert result.provider == "backup"
    assert len(primary.calls) == 2  # original call + one retry, then move on
    assert metrics.count("llm_errors", "timeout") == 2


async def test_unparseable_output_is_reasked() -> None:
    primary = FakeProvider("primary", ["Sure! Here is the JSON you asked for", answer()])
    interp, _ = interpreter(primary)
    result = await interp.interpret_note(NOTE, 0, 500)
    assert (result.guardrail, result.attempts) == ("pass_after_retry", 2)


async def test_guardrail_rejection_is_fed_back_to_the_model() -> None:
    primary = FakeProvider("primary", [answer(factor=20), answer(factor=0.2)])
    interp, metrics = interpreter(primary)
    result = await interp.interpret_note(NOTE, 0, 500)
    assert result.directive == SolarReduction(hours=(13, 14), factor=0.2)
    assert metrics.count("guardrail_rejects", "factor_looks_like_percent") == 1
    assert "factor_looks_like_percent" in primary.calls[1][-1]["content"]


async def test_unsupported_directive_type_never_reaches_the_optimizer() -> None:
    primary = FakeProvider("primary", [answer("shed_load")])
    interp, _ = interpreter(primary)
    result = await interp.interpret_note("Shed 40 kWh of load at 5 PM.", 0, 500)
    assert result.directive == NoOp()
    assert result.provider == "heuristic"


async def test_all_providers_down_degrades_to_the_deterministic_reader() -> None:
    primary = FakeProvider("primary", [LLMError("provider")])
    backup = FakeProvider("backup", [LLMError("rate_limit")])
    interp, metrics = interpreter(primary, backup)
    first = await interp.interpret_note(NOTE, 0, 500)
    assert first.directive == SolarReduction(hours=(13, 14), factor=0.2)
    assert (first.provider, first.guardrail) == ("heuristic", "fallback")
    await interp.interpret_note(NOTE, 0, 500)
    assert len(primary.calls) == 4  # fallback results are never cached
    assert metrics.count("provider_used", "heuristic") == 2


async def test_no_providers_configured_is_degraded_mode() -> None:
    interp, _ = interpreter()
    assert interp.degraded
    result = await interp.interpret_note("Do not charge the battery between 2 PM and 4 PM.", 0, 200)
    assert result.directive == NoCharge(hours=(14, 15))


async def test_request_deadline_stops_the_chain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(llm_module, "_MIN_CALL_SECONDS", 0.01)
    slow = FakeProvider("slow", [30])
    backup = FakeProvider("backup", [30])
    interp, metrics = interpreter(slow, backup, llm_timeout_s=0.1, llm_total_budget_s=0.25)
    started = asyncio.get_running_loop().time()
    result = await interp.interpret_note(NOTE, 0, 500)
    assert asyncio.get_running_loop().time() - started < 1.0
    assert result.provider == "heuristic"
    assert metrics.count("llm_errors", "timeout") >= 2


async def test_validated_answers_are_cached_per_note_and_capacity() -> None:
    primary = FakeProvider("primary", [answer()])
    interp, metrics = interpreter(primary)
    first = await interp.interpret_note(NOTE, 0, 500)
    second = await interp.interpret_note("  " + NOTE.upper() + " ", 2, 500)
    assert (first.cache_hit, second.cache_hit) == (False, True)
    assert len(primary.calls) == 1
    await interp.interpret_note(NOTE, 0, 300)  # different capacity -> different key
    assert len(primary.calls) == 2
    assert metrics.count("cache_hits") == 1


async def test_identical_concurrent_notes_share_one_call() -> None:
    primary = FakeProvider("primary", [0.05])
    interp, _ = interpreter(primary)
    results = await asyncio.gather(*(interp.interpret_note(NOTE, 0, 500) for _ in range(25)))
    assert len(primary.calls) == 1
    assert {r.directive for r in results} == {SolarReduction(hours=(13, 14), factor=0.2)}


async def test_results_keep_note_order() -> None:
    class ByNote(FakeProvider):
        async def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
            note = json.loads(messages[1]["content"])["note"]
            await asyncio.sleep(0.05 if "slow" in note else 0)
            return NO_OP if "slow" in note else answer("no_charge_window", hours=[2], factor=None)

    interp, _ = interpreter(ByNote("primary", [""]), crosscheck_enabled=False)
    results = await interp.interpret_all(["slow note", "fast note", "slow again"], 200)
    assert [r.directive.directive_type for r in results] == ["no_op", "no_charge_window", "no_op"]


async def test_crosscheck_disagreement_triggers_one_reask_and_the_model_decides() -> None:
    primary = FakeProvider("primary", [answer(hours=[13, 14, 15]), answer(hours=[13, 14])])
    interp, metrics = interpreter(primary)
    result = await interp.interpret_note(NOTE, 0, 500)
    assert result.directive == SolarReduction(hours=(13, 14), factor=0.2)
    assert "[13, 14]" in primary.calls[1][-1]["content"]
    assert metrics.count("crosscheck_changed") == 1

    stubborn = FakeProvider("primary", [answer(hours=[13, 14, 15])])
    interp, _ = interpreter(stubborn)
    result = await interp.interpret_note(NOTE, 0, 500)
    assert result.directive == SolarReduction(hours=(13, 14, 15), factor=0.2)
    assert len(stubborn.calls) == 2  # asked once more, never overridden


def test_crosscheck_only_speaks_when_confident() -> None:
    agree = SolarReduction(hours=(13, 14), factor=0.2)
    assert crosscheck_hint(NOTE, 500, agree) is None
    assert crosscheck_hint(NOTE, 500, SolarReduction(hours=(13, 14), factor=0.8)) is not None
    assert crosscheck_hint(NOTE, 500, NoOp()) is not None
    assert crosscheck_hint("The cafeteria menu changes tomorrow.", 500, NoOp()) is None
    assert (
        crosscheck_hint(
            "Reduce solar from 2 to 4.", 500, SolarReduction(hours=(14, 15), factor=0.5)
        )
        is None
    )


def test_parse_model_json() -> None:
    assert parse_model_json('```json\n{"a": 1}\n```') == {"a": 1}
    for bad in ("", "[1, 2]", "not json", '"text"'):
        with pytest.raises(LLMError):
            parse_model_json(bad)


class _Completions:
    def __init__(self, outcomes: list[Any]) -> None:
        self.outcomes, self.seen = outcomes, []

    async def create(self, **kwargs: Any) -> Any:
        self.seen.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _bad_request(message: str) -> openai.BadRequestError:
    request = httpx.Request("POST", "https://llm.invalid/v1/chat/completions")
    return openai.BadRequestError(message, response=httpx.Response(400, request=request), body=None)


def _completion(content: str | None) -> Any:
    from types import SimpleNamespace as NS

    return NS(
        choices=[NS(message=NS(content=content))], usage=NS(prompt_tokens=900, completion_tokens=40)
    )


async def test_rejected_optional_parameters_are_dropped_and_remembered() -> None:
    completions = _Completions(
        [
            _bad_request("Unsupported parameter: 'reasoning_effort'"),
            _bad_request("Unknown parameter: prompt_cache_key"),
            _completion(answer()),
            _completion(answer()),
        ]
    )
    client: Any = type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()
    metrics = Metrics()
    provider = OpenAICompatProvider(
        "openai:test",
        client,
        "test",
        metrics,
        max_output_tokens=100,
        reasoning_effort="none",
        prompt_cache_key="k",
    )
    assert json.loads(await provider.complete([], timeout=1))["factor"] == 0.2
    await provider.complete([], timeout=1)
    assert "reasoning_effort" in completions.seen[0]
    assert (
        "reasoning_effort" not in completions.seen[2]
        and "prompt_cache_key" not in completions.seen[2]
    )
    assert "reasoning_effort" not in completions.seen[3]  # remembered, not re-negotiated
    assert metrics.count("tokens_in") == 1800


async def test_provider_failures_are_classified() -> None:
    request = httpx.Request("POST", "https://llm.invalid")
    cases = [
        (openai.APITimeoutError(request=request), "timeout"),
        (
            openai.RateLimitError(
                "slow down", response=httpx.Response(429, request=request), body=None
            ),
            "rate_limit",
        ),
        (openai.APIConnectionError(request=request), "provider"),
        (_bad_request("messages: malformed"), "provider"),
        (_completion(None), "parse"),
    ]
    for outcome, kind in cases:
        completions = _Completions([outcome])
        client: Any = type("C", (), {"chat": type("Ch", (), {"completions": completions})()})()
        provider = OpenAICompatProvider("p", client, "m", Metrics(), max_output_tokens=10)
        with pytest.raises(LLMError) as caught:
            await provider.complete([], timeout=1)
        assert caught.value.kind == kind
