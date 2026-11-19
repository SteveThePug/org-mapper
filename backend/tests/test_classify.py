"""AI fallback for unmatched group labels: asked once, saved, and never trusted blindly."""

from types import SimpleNamespace
from typing import Any

from radar.config import DomainConfig
from radar.db import Database
from radar.llm.classify import LabelClassifier
from radar.llm.gateway import LlmGateway
from radar.pipeline.normalise import classify_groups
from radar.settings import Settings

DOMAIN = DomainConfig.model_validate(
    {
        "titles": {"partner": ["partner"]},
        "taxonomy": {
            "mergers-acquisitions": {"label": "Mergers & Acquisitions", "include": ["merger", "\\bm&a\\b"]},
            "tax": {"label": "Tax", "include": ["\\btax"]},
        },
        "segments": [
            {"id": "ma", "name": "M&A partners", "titles": ["partner"], "groups": ["mergers-acquisitions"]}
        ],
    }
)


class FakeCompletions:
    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        usage = SimpleNamespace(model_dump=lambda: {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.0})
        choice = SimpleNamespace(message=SimpleNamespace(content=self.content), finish_reason="stop")
        return SimpleNamespace(choices=[choice], usage=usage, model=kwargs["model"])


def classifier(db: Database, completions: FakeCompletions, api_key: str = "test-key") -> LabelClassifier:
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    gateway = LlmGateway(db, Settings(openrouter_api_key=api_key), client)  # type: ignore[arg-type]
    return LabelClassifier(db, gateway)


async def test_unmatched_label_is_asked_once_and_saved(db: Database) -> None:
    fake = FakeCompletions('{"tags": ["mergers-acquisitions", "made-up-tag"]}')
    labels = ["Mergers & Acquisitions", "Sponsor-side Deal Advisory"]

    learned = await classifier(db, fake).learned_tags(labels, DOMAIN)

    # Only the label the keywords missed was sent, and the invented tag was dropped.
    assert len(fake.calls) == 1
    assert "Sponsor-side Deal Advisory" in fake.calls[0]["messages"][1]["content"]
    assert learned == {"sponsor-side deal advisory": ["mergers-acquisitions"]}

    # A fresh classifier (e.g. the next crawl) reads the saved answer and makes no call.
    again = await classifier(db, fake).learned_tags(labels, DOMAIN)
    assert again == learned
    assert len(fake.calls) == 1


async def test_no_api_key_means_keywords_only(db: Database) -> None:
    fake = FakeCompletions('{"tags": ["tax"]}')
    labels = ["Odd Label One", "Odd Label Two"]

    learned = await classifier(db, fake, api_key="").learned_tags(labels, DOMAIN)

    assert learned == {}
    assert fake.calls == []
    # One refusal row in the ledger, not one per label.
    assert await db["llm_calls"].count_documents({"task": "classify_label"}) == 1


def test_learned_tags_only_fill_gaps() -> None:
    learned = {"deal advisory": ["mergers-acquisitions"], "tax structuring": ["mergers-acquisitions"]}
    # "Tax Structuring" matches a keyword rule, so the rule wins and the saved answer is ignored.
    tags = classify_groups(["Deal Advisory", "Tax Structuring"], DOMAIN.taxonomy, learned)
    assert tags == ["mergers-acquisitions", "tax"]
