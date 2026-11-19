"""The only code that talks to an LLM. Every call goes: toggle -> cache -> budget -> call -> ledger.

Because everything funnels through `LlmGateway.run`, cost control and cost reporting can't be
bypassed by a new feature: a feature gets a task name, a prompt version and an output schema,
and the gateway does the rest.
"""

import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from openai import AsyncOpenAI, OpenAIError
from pydantic import BaseModel, Field, ValidationError
from pymongo.errors import DuplicateKeyError

from radar.db import Database
from radar.models import Doc, LlmCall
from radar.settings import Settings

log = logging.getLogger(__name__)

MAX_OUTPUT_TOKENS = 4000
# Held against the daily budget while a call is in flight, then replaced by the real cost. It
# stops many simultaneous requests from all passing the budget check before any of them is billed.
RESERVE_USD = 0.05


class LlmRefused(Exception):
    """The call was not made. `reason` is shown to the user as-is."""

    def __init__(self, reason: str, status: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class TaskSettings(BaseModel):
    enabled: bool
    model: str
    description: str


class FixedCost(BaseModel):
    name: str
    monthly_usd: float


class RuntimeSettings(Doc):
    """Admin-editable at runtime (stored in Mongo), so features can be switched off instantly."""

    id: str = Field(default="runtime", alias="_id")
    daily_budget_usd: float
    tasks: dict[str, TaskSettings]
    fixed_costs: list[FixedCost]


def default_runtime_settings(settings: Settings) -> RuntimeSettings:
    return RuntimeSettings(
        daily_budget_usd=settings.llm_daily_budget_usd,
        tasks={
            "call_prep_brief": TaskSettings(
                enabled=True,
                model=settings.llm_model_brief,
                description="One-screen 'before you call' brief for a partner, generated on request and cached.",
            ),
            "classify_label": TaskSettings(
                enabled=True,
                model=settings.llm_model_cheap,
                description="Tags a group label no keyword rule matches. Once per distinct label, saved; label text only.",
            ),
            "org_setup": TaskSettings(
                enabled=True,
                model=settings.llm_model_brief,
                description="Fills in the scraper settings when an admin adds an organisation, from a summary of its website. Public site structure only.",
            ),
            "market_digest": TaskSettings(
                enabled=False,
                model=settings.llm_model_cheap,
                description="Short written digest of the week's market moves (exploratory: off by default).",
            ),
        },
        fixed_costs=[
            FixedCost(name="Server (1 small VM, e.g. 2 vCPU / 4 GB)", monthly_usd=8.0),
            FixedCost(name="Backups / object storage", monthly_usd=1.0),
            FixedCost(name="Domain + TLS (Caddy, Let's Encrypt)", monthly_usd=1.0),
        ],
    )


async def load_runtime_settings(db: Database, settings: Settings) -> RuntimeSettings:
    doc = await db["app_settings"].find_one({"_id": "runtime"})
    defaults = default_runtime_settings(settings)
    if doc is None:
        # Upsert, so two first requests at the same moment don't both try to insert.
        await db["app_settings"].update_one(
            {"_id": "runtime"}, {"$setOnInsert": defaults.to_mongo()}, upsert=True
        )
        return defaults
    stored = RuntimeSettings.model_validate(doc)
    for name, task in defaults.tasks.items():  # tasks added in a newer release
        stored.tasks.setdefault(name, task)
    return stored


async def save_runtime_settings(db: Database, runtime: RuntimeSettings) -> None:
    await db["app_settings"].replace_one({"_id": "runtime"}, runtime.to_mongo(), upsert=True)


@dataclass
class LlmResult[T: BaseModel]:
    output: T
    output_id: str
    cached: bool
    model: str
    cost_usd: float
    created_at: datetime


class LlmGateway:
    def __init__(self, db: Database, settings: Settings, client: AsyncOpenAI | None = None) -> None:
        self.db = db
        self.settings = settings
        self._client = client

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            self._client = AsyncOpenAI(
                api_key=self.settings.openrouter_api_key, base_url=self.settings.openrouter_base_url
            )
        return self._client

    async def run[T: BaseModel](
        self,
        task: str,
        prompt_version: str,
        system: str,
        payload: dict[str, Any],
        schema: type[T],
        subject_id: str | None = None,
        user: str | None = None,
        cache_payload: dict[str, Any] | None = None,
    ) -> LlmResult[T]:
        """`cache_payload`, if given, is hashed for the cache key instead of `payload`: callers use
        it to leave out facts that change without changing the answer (e.g. a "last checked" date)."""
        runtime = await load_runtime_settings(self.db, self.settings)
        config = runtime.tasks[task]
        hashed = payload if cache_payload is None else cache_payload
        input_hash = _hash(
            {"task": task, "v": prompt_version, "model": config.model, "s": system, "p": hashed}
        )
        ledger = LlmCall(
            id=uuid.uuid4().hex,
            ts=datetime.now(UTC),
            task=task,
            model=config.model,
            prompt_version=prompt_version,
            input_hash=input_hash,
            subject_id=subject_id,
            user=user,
            cache_hit=False,
            status="ok",
        )

        if not config.enabled:
            await self._log(ledger, status="disabled")
            raise LlmRefused("This AI feature is switched off by an administrator.", "disabled")

        cached = await self.db["ai_outputs"].find_one({"_id": input_hash})
        if cached is not None:
            await self._log(ledger, cache_hit=True)
            return LlmResult(
                output=schema.model_validate(cached["output"]),
                output_id=input_hash,
                cached=True,
                model=cached["model"],
                cost_usd=0.0,
                created_at=cached["created_at"],
            )

        if not self.settings.llm_enabled:
            await self._log(ledger, status="disabled", error="no OPENROUTER_API_KEY")
            raise LlmRefused("AI features are not configured (no OpenRouter API key).", "unavailable")
        day = f"{midnight_utc():%Y-%m-%d}"
        if not await reserve_budget(self.db, day, RESERVE_USD, runtime.daily_budget_usd):
            await self._log(ledger, status="over_budget")
            raise LlmRefused(
                f"Today's AI budget (${runtime.daily_budget_usd:.2f}) has been reached.", "over_budget"
            )

        started = time.monotonic()
        usage: dict[str, Any] = {}  # added up over every attempt, including failed ones
        try:
            output, model_used = await self._complete(config.model, system, payload, schema, usage)
        except (OpenAIError, ValidationError, ValueError) as exc:
            cost_usd = float(usage.get("cost") or 0.0)
            await settle_budget(self.db, day, RESERVE_USD, cost_usd)
            await self._log(
                ledger,
                status="error",
                error=str(exc)[:500],
                started=started,
                tokens_in=usage.get("prompt_tokens", 0),
                tokens_out=usage.get("completion_tokens", 0),
                cost_usd=cost_usd,
            )
            log.warning("LLM task %s failed: %s", task, exc)
            raise LlmRefused(
                "The AI provider failed to produce a valid answer; please retry.", "error"
            ) from exc

        cost_usd = float(usage.get("cost") or 0.0)  # OpenRouter reports the actual cost in `usage`
        await settle_budget(self.db, day, RESERVE_USD, cost_usd)
        await self._log(
            ledger,
            model=model_used,
            tokens_in=usage.get("prompt_tokens", 0),
            tokens_out=usage.get("completion_tokens", 0),
            cost_usd=cost_usd,
            started=started,
        )
        now = datetime.now(UTC)
        # Upsert, not insert: if an identical request finished first, keep its copy instead of
        # failing with a duplicate key after the call has been paid for.
        await self.db["ai_outputs"].update_one(
            {"_id": input_hash},
            {
                "$setOnInsert": {
                    "task": task,
                    "subject_id": subject_id,
                    "prompt_version": prompt_version,
                    "model": model_used,
                    "output": output.model_dump(),
                    "created_at": now,
                    "llm_call_id": ledger.id,
                    "cost_usd": cost_usd,
                }
            },
            upsert=True,
        )
        return LlmResult(
            output=output,
            output_id=input_hash,
            cached=False,
            model=model_used,
            cost_usd=cost_usd,
            created_at=now,
        )

    async def _complete[T: BaseModel](
        self, model: str, system: str, payload: dict[str, Any], schema: type[T], usage: dict[str, Any]
    ) -> tuple[T, str]:
        """Returns (output, model used). Token counts and cost of every attempt are added to `usage`."""
        # `models` = OpenRouter falls back down the list on errors/rate limits. Low reasoning effort keeps
        # "thinking" models from spending the token budget before they write the answer.
        extra: dict[str, Any] = {
            "models": [model, *self.settings.llm_fallback_models],
            "reasoning": {"effort": "low"},
            "usage": {"include": True},  # ask OpenRouter to report the cost of each call
        }
        if self.settings.llm_deny_data_collection:
            extra["provider"] = {"data_collection": "deny"}
        messages: list[Any] = [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        last_error: Exception | None = None
        for _attempt in range(2):  # one retry if the model returns invalid JSON
            response = await self.client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=0.2,
                max_tokens=MAX_OUTPUT_TOKENS,
                response_format={"type": "json_object"},
                extra_body=extra,
            )
            choice = response.choices[0]
            content = choice.message.content or ""
            if response.usage:
                for key, value in response.usage.model_dump().items():
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
            try:
                return schema.model_validate(json.loads(_json_object(content))), response.model
            except (ValueError, ValidationError) as exc:
                last_error = ValueError(
                    f"{exc} (model={response.model}, finish_reason={choice.finish_reason})"
                )
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {"role": "user", "content": f"Invalid: {exc}. Reply with only the JSON object."}
                )
        raise ValueError(f"invalid model output: {last_error}")

    async def _log(self, ledger: LlmCall, started: float | None = None, **fields: Any) -> None:
        """Write one ledger row. `started` is a `time.monotonic()` reading, used to record latency."""
        if started is not None:
            fields["latency_ms"] = int((time.monotonic() - started) * 1000)
        record = ledger.model_copy(update=fields)
        await self.db["llm_calls"].insert_one(record.to_mongo())


async def reserve_budget(db: Database, day: str, amount: float, budget: float) -> bool:
    """Atomically add `amount` to today's committed spend, unless that would exceed `budget`.

    One document per day holds the running total. The filter only matches while there is room;
    when it doesn't match, the upsert tries to insert a second document with the same `_id`,
    and the duplicate-key error is how Mongo tells us the budget is full.
    """
    if amount > budget:
        return False
    try:
        await db["llm_budget"].update_one(
            {"_id": day, "committed_usd": {"$lte": budget - amount}},
            {"$inc": {"committed_usd": amount}},
            upsert=True,
        )
    except DuplicateKeyError:
        return False
    return True


async def settle_budget(db: Database, day: str, reserved: float, actual: float) -> None:
    """Swap a reservation for the real cost once it is known."""
    await db["llm_budget"].update_one({"_id": day}, {"$inc": {"committed_usd": actual - reserved}})


async def spend_since(db: Database, since: datetime) -> float:
    rows = await (
        await db["llm_calls"].aggregate(
            [{"$match": {"ts": {"$gte": since}}}, {"$group": {"_id": None, "cost": {"$sum": "$cost_usd"}}}]
        )
    ).to_list()
    return float(rows[0]["cost"]) if rows else 0.0


def midnight_utc() -> datetime:
    now = datetime.now(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _hash(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _json_object(text: str) -> str:
    """Models sometimes wrap JSON in prose or ``` fences; take the outermost object."""
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("no JSON object in model output")
    return match.group(0)
