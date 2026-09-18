"""HTTP contract end to end, with the language model replaced by a scripted provider."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from app.config import Settings
from app.llm import LLMError, NoteInterpreter
from app.main import create_app
from app.metrics import Metrics
from app.schemas import HourPlan, directive_from_adjustment
from app.validator import replay_and_check
from tests.conftest import PUBLIC_CASES, PublicCase

# What a well-behaved model would answer for every official public note.
GROUND_TRUTH: dict[str, dict[str, Any]] = {
    note: entry
    for case in PUBLIC_CASES
    for note, entry in zip(
        case.raw_input["operator_notes"], case.expected["directive_interpretation"], strict=True
    )
}


class OracleProvider:
    """Answers public notes with the reference interpretation, in the model's flat format."""

    name = "openai:oracle"

    def __init__(self) -> None:
        self.fail_with: LLMError | None = None
        self.garbage = False

    async def complete(self, messages: list[dict[str, str]], *, timeout: float) -> str:
        if self.fail_with is not None:
            raise self.fail_with
        if self.garbage:
            return '{"directive_type": "reticulate_splines", "hours": "all of them"}'
        asked = json.loads(messages[1]["content"])
        truth = GROUND_TRUTH.get(asked["note"])
        adjustment = (truth or {}).get("structured_adjustment") or {}
        return json.dumps(
            {
                "note_index": asked["note_index"],
                "applies": bool(truth and truth["applies"]),
                "directive_type": truth["directive_type"] if truth else "no_op",
                "hours": adjustment.get("hours", []),
                "factor": adjustment.get("factor"),
                "minimum_energy_kwh": adjustment.get("minimum_energy_kwh"),
                "max_grid_kwh": adjustment.get("max_grid_kwh"),
                "explanation": "oracle",
            }
        )


@pytest.fixture
def oracle() -> OracleProvider:
    return OracleProvider()


@pytest.fixture
async def client(oracle: OracleProvider) -> AsyncIterator[httpx.AsyncClient]:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    interpreter = NoteInterpreter(settings, [oracle], Metrics())
    transport = httpx.ASGITransport(app=create_app(settings, interpreter))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def test_health_needs_no_keys_and_no_dependencies() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        response = await http.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_public_cases_end_to_end(client: httpx.AsyncClient, public_case: PublicCase) -> None:
    response = await client.post("/optimize-energy", json=public_case.raw_input)
    assert response.status_code == 200
    body = response.json()

    assert list(body) == [
        "scenario_id", "directive_interpretation", "hourly_plan",
        "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh", "plan_summary",
    ]  # fmt: skip
    assert body["scenario_id"] == public_case.id

    machine_checked = ("note_index", "applies", "directive_type", "structured_adjustment")
    assert [{k: e[k] for k in machine_checked} for e in body["directive_interpretation"]] == [
        {k: e[k] for k in machine_checked} for e in public_case.expected["directive_interpretation"]
    ]
    assert all(e["explanation"] for e in body["directive_interpretation"])

    # Replay independently: reference directives, strict tolerance.
    violations = replay_and_check(
        public_case.request,
        public_case.directives,
        [HourPlan.model_validate(p) for p in body["hourly_plan"]],
        total_grid_kwh=body["total_grid_kwh"],
        total_cost_bdt=body["total_cost_bdt"],
        peak_grid_kwh=body["peak_grid_kwh"],
        tol=1e-6,
    )
    assert violations == []
    assert body["total_cost_bdt"] == pytest.approx(public_case.expected["total_cost_bdt"], abs=0.01)
    assert isinstance(body["plan_summary"], str) and body["plan_summary"]


async def test_body_is_accepted_without_a_json_content_type(
    client: httpx.AsyncClient, sample_input: dict[str, Any]
) -> None:
    response = await client.post(
        "/optimize-energy", content=json.dumps(sample_input), headers={"content-type": "text/plain"}
    )
    assert response.status_code == 200


MALFORMED_BODIES = {
    "not_json": b"{this is not json",
    "empty": b"",
    "json_array": b"[1, 2, 3]",
    "json_string": b'"hello"',
    "json_null": b"null",
    "invalid_utf8": b"\xff\xfe\x00",
    "deeply_nested": b"[" * 100_000,
}


@pytest.mark.parametrize("name", MALFORMED_BODIES)
async def test_malformed_bodies_are_400(client: httpx.AsyncClient, name: str) -> None:
    response = await client.post("/optimize-energy", content=MALFORMED_BODIES[name])
    assert response.status_code == 400
    assert response.json()["error"] == "bad_request"


def _mutations() -> dict[str, Any]:
    def set_path(*path: Any, value: Any) -> Any:
        def mutate(data: dict[str, Any]) -> None:
            target: Any = data
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = value

        return mutate

    return {
        "23_hours": lambda d: d["hours"].pop(),
        "25_hours": lambda d: d["hours"].append(dict(d["hours"][0])),
        "duplicate_hour": set_path("hours", 5, "hour", value=4),
        "hour_24": set_path("hours", 23, "hour", value=24),
        "negative_demand": set_path("hours", 0, "demand_kwh", value=-1),
        "string_demand": set_path("hours", 0, "demand_kwh", value="90"),
        "null_tariff": set_path("hours", 0, "tariff_bdt_per_kwh", value=None),
        "zero_notes": set_path("operator_notes", value=[]),
        "four_notes": set_path("operator_notes", value=["a", "b", "c", "d"]),
        "whitespace_note": set_path("operator_notes", value=["  \n "]),
        "missing_battery": lambda d: d.pop("battery"),
        "missing_scenario_id": lambda d: d.pop("scenario_id"),
        "negative_capacity": set_path("battery", "capacity_kwh", value=-10),
    }


@pytest.mark.parametrize("name", _mutations())
async def test_structurally_invalid_requests_are_400(
    client: httpx.AsyncClient, sample_input: dict[str, Any], name: str
) -> None:
    _mutations()[name](sample_input)
    response = await client.post("/optimize-energy", json=sample_input)
    assert response.status_code == 400
    body = response.json()
    assert body["error"] == "bad_request" and body["detail"] and body["request_id"]


async def test_non_finite_numbers_are_400(
    client: httpx.AsyncClient, sample_input: dict[str, Any]
) -> None:
    for literal in ("NaN", "Infinity", "1e999"):
        raw = json.dumps(sample_input).replace('"demand_kwh": 85', f'"demand_kwh": {literal}', 1)
        assert literal in raw
        response = await client.post("/optimize-energy", content=raw)
        assert response.status_code == 400, literal


async def test_contradictory_battery_is_422(
    client: httpx.AsyncClient, sample_input: dict[str, Any]
) -> None:
    sample_input["battery"]["initial_energy_kwh"] = 1  # below minimum_energy_kwh
    response = await client.post("/optimize-energy", json=sample_input)
    assert response.status_code == 422
    assert response.json()["error"] == "unprocessable"


async def test_oversized_bodies_are_rejected_before_parsing(
    client: httpx.AsyncClient, sample_input: dict[str, Any]
) -> None:
    sample_input["scenario_id"] = "x" * 1_000_000
    response = await client.post("/optimize-energy", json=sample_input)
    assert response.status_code == 400


async def test_long_notes_and_unknown_fields_do_not_fail_a_valid_case(
    client: httpx.AsyncClient, sample_input: dict[str, Any]
) -> None:
    sample_input["operator_notes"][2] = "The library newsletter says: " + "blah " * 2000
    sample_input["harness"] = {"attempt": 3}
    response = await client.post("/optimize-energy", json=sample_input)
    assert response.status_code == 200
    assert response.json()["directive_interpretation"][2]["directive_type"] == "no_op"


@pytest.mark.parametrize("kind", ["timeout", "rate_limit", "provider"])
async def test_provider_failure_is_never_a_5xx(
    client: httpx.AsyncClient, oracle: OracleProvider, public_case: PublicCase, kind: str
) -> None:
    oracle.fail_with = LLMError(kind)
    response = await client.post("/optimize-energy", json=public_case.raw_input)
    assert response.status_code == 200
    body = response.json()
    assert len(body["directive_interpretation"]) == len(public_case.raw_input["operator_notes"])
    assert len(body["hourly_plan"]) == 24


async def test_malformed_model_output_is_contained(
    client: httpx.AsyncClient, oracle: OracleProvider, sample_input: dict[str, Any]
) -> None:
    oracle.garbage = True
    sample_input["operator_notes"] = ["Please reticulate the splines before lunch."]
    response = await client.post("/optimize-energy", json=sample_input)
    assert response.status_code == 200
    entry = response.json()["directive_interpretation"][0]
    assert (entry["directive_type"], entry["applies"], entry["structured_adjustment"]) == (
        "no_op", False, None,
    )  # fmt: skip


async def test_twenty_concurrent_requests_all_succeed(client: httpx.AsyncClient) -> None:
    inputs = [PUBLIC_CASES[i % len(PUBLIC_CASES)].raw_input for i in range(20)]
    responses = await asyncio.gather(*(client.post("/optimize-energy", json=i) for i in inputs))
    assert [r.status_code for r in responses] == [200] * 20
    assert [r.json()["scenario_id"] for r in responses] == [i["scenario_id"] for i in inputs]


async def test_unexpected_errors_are_controlled_500s(
    client: httpx.AsyncClient, sample_input: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*_: Any) -> None:
        raise RuntimeError("secret-path C:/srv/app/optimizer.py sk-THISMUSTNOTLEAK123")

    monkeypatch.setattr("app.routers.core.plan_schedule", explode)
    response = await client.post("/optimize-energy", json=sample_input)
    assert response.status_code == 500
    assert response.json()["error"] == "internal_error"
    for leak in ("Traceback", "RuntimeError", "optimizer.py", "sk-", "secret-path"):
        assert leak not in response.text


async def test_unknown_routes_and_methods_use_the_envelope(client: httpx.AsyncClient) -> None:
    assert (await client.get("/nope")).json()["error"] == "not_found"
    wrong_method = await client.get("/optimize-energy")
    assert wrong_method.status_code == 405
    assert wrong_method.json()["error"] == "method_not_allowed"


async def test_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/health", headers={"X-Request-ID": "client-42"})
    assert response.headers["X-Request-ID"] == "client-42"
    assert (await client.get("/health")).headers["X-Request-ID"]


async def test_optional_endpoints_are_labelled_and_can_be_disabled(
    client: httpx.AsyncClient, sample_input: dict[str, Any]
) -> None:
    await client.post("/optimize-energy", json=sample_input)
    stats = (await client.get("/stats")).json()
    assert stats["counters"]["requests_by_status.200"] >= 1
    assert "Cloud cover" not in json.dumps(stats)  # aggregates only, never note text

    interpreted = await client.post(
        "/interpret",
        json={"operator_notes": sample_input["operator_notes"], "battery_capacity_kwh": 220},
    )
    assert interpreted.status_code == 200
    first = interpreted.json()["notes"][0]
    assert first["provider"] == "openai:oracle"
    assert first["interpretation"]["directive_type"] == "solar_reduction"
    assert directive_from_adjustment(
        first["interpretation"]["directive_type"], first["interpretation"]["structured_adjustment"]
    )

    spec = (await client.get("/openapi.json")).json()
    assert spec["paths"]["/stats"]["get"]["summary"].startswith("[OPTIONAL]")
    assert spec["paths"]["/interpret"]["post"]["summary"].startswith("[OPTIONAL]")
    assert "OptimizeRequest" in spec["components"]["schemas"]

    settings = Settings(_env_file=None, enable_optional_endpoints=False)  # type: ignore[call-arg]
    transport = httpx.ASGITransport(app=create_app(settings))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as bare:
        assert (await bare.get("/stats")).status_code == 404
        assert (await bare.get("/health")).status_code == 200
