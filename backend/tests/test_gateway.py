"""The LLM gateway's guarantees: toggles, caching, budget, and a ledger row for everything."""

import asyncio
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel

from radar.db import Database
from radar.llm.gateway import LlmGateway, LlmRefused, load_runtime_settings, save_runtime_settings
from radar.settings import Settings


class Answer(BaseModel):
    text: str


@dataclass
class FakeCompletions:
    content: str = '```json\n{"text": "hello"}\n```'
    cost: float = 0.002
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        usage = SimpleNamespace(
            model_dump=lambda: {"prompt_tokens": 120, "completion_tokens": 30, "cost": self.cost}
        )
        message = SimpleNamespace(content=self.content)
        choice = SimpleNamespace(message=message, finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage, model=kwargs["model"])


def gateway(db: Database, completions: FakeCompletions, **settings: Any) -> LlmGateway:
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return LlmGateway(db, Settings(openrouter_api_key="test-key", **settings), client)  # type: ignore[arg-type]


async def run(gw: LlmGateway, payload: dict[str, Any] | None = None) -> Any:
    return await gw.run("call_prep_brief", "v1", "system", payload or {"facts": [1]}, Answer, "p1", "u@x")


async def ledger(db: Database) -> list[dict[str, Any]]:
    return [doc async for doc in db["llm_calls"].find().sort([("ts", 1)])]


async def test_second_identical_request_is_a_free_cache_hit(db: Database) -> None:
    completions = FakeCompletions()
    gw = gateway(db, completions)

    first = await run(gw)
    second = await run(gw)

    assert first.output.text == second.output.text == "hello"
    assert (first.cached, second.cached) == (False, True)
    assert len(completions.calls) == 1
    rows = await ledger(db)
    assert [(r["cache_hit"], r["cost_usd"], r["tokens_in"]) for r in rows] == [
        (False, 0.002, 120),
        (True, 0.0, 0),
    ]


async def test_changed_input_is_not_served_from_cache(db: Database) -> None:
    completions = FakeCompletions()
    gw = gateway(db, completions)
    await run(gw, {"facts": [1]})
    await run(gw, {"facts": [1, 2]})
    assert len(completions.calls) == 2


async def test_disabled_task_makes_no_call_but_is_logged(db: Database) -> None:
    settings = Settings(openrouter_api_key="test-key")
    runtime = await load_runtime_settings(db, settings)
    runtime.tasks["call_prep_brief"].enabled = False
    await save_runtime_settings(db, runtime)
    completions = FakeCompletions()

    with pytest.raises(LlmRefused) as refused:
        await run(gateway(db, completions))

    assert refused.value.status == "disabled"
    assert completions.calls == []
    assert [r["status"] for r in await ledger(db)] == ["disabled"]


async def test_daily_budget_is_a_hard_stop(db: Database) -> None:
    completions = FakeCompletions(cost=0.6)
    gw = gateway(db, completions, llm_daily_budget_usd=1.0)
    await run(gw, {"n": 1})
    await run(gw, {"n": 2})  # spent 1.2 now
    with pytest.raises(LlmRefused) as refused:
        await run(gw, {"n": 3})
    assert refused.value.status == "over_budget"
    assert len(completions.calls) == 2


async def test_concurrent_requests_cannot_overshoot_the_budget(db: Database) -> None:
    completions = FakeCompletions(cost=0.06)
    gw = gateway(db, completions, llm_daily_budget_usd=0.12)
    results = await asyncio.gather(*(run(gw, {"n": i}) for i in range(6)), return_exceptions=True)
    refused = [r for r in results if isinstance(r, LlmRefused)]
    # Each in-flight call holds a reservation, so they can't all pass the check before any is billed.
    assert len(completions.calls) <= 2
    assert len(refused) >= 4 and {r.status for r in refused} == {"over_budget"}


async def test_identical_concurrent_requests_do_not_crash(db: Database) -> None:
    gw = gateway(db, FakeCompletions())
    first, second = await asyncio.gather(run(gw), run(gw))
    assert first.output_id == second.output_id
    assert await db["ai_outputs"].count_documents({}) == 1


async def test_invalid_json_is_retried_once_then_refused(db: Database) -> None:
    completions = FakeCompletions(content="I am not JSON")
    with pytest.raises(LlmRefused) as refused:
        await run(gateway(db, completions))
    assert refused.value.status == "error"
    assert len(completions.calls) == 2
    rows = await ledger(db)
    assert [r["status"] for r in rows] == ["error"]
    # Both failed attempts were billed, so both are counted.
    assert rows[0]["cost_usd"] == pytest.approx(0.004)
    assert rows[0]["tokens_in"] == 240


async def test_no_api_key_means_no_call(db: Database) -> None:
    gw = LlmGateway(db, Settings(openrouter_api_key=""))
    with pytest.raises(LlmRefused) as refused:
        await run(gw)
    assert refused.value.status == "unavailable"
