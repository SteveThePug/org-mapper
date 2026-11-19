"""AI fallback for group labels that no keyword rule in the domain taxonomy matches.

Keywords stay the first choice: free, instant and easy to explain. Only a label that matches no
rule ("Sponsor-side Deal Advisory", "Striker") is sent to the LLM, once. The answer is saved in
`label_tags` keyed by the label and a fingerprint of the taxonomy, so:

- each distinct label costs at most one call, ever (there are hundreds of labels, not thousands);
- the same label always gets the same tags, so a re-crawl can't invent a "groups changed" event;
- editing the taxonomy changes the fingerprint, and labels are asked about again.

Only the label text goes to the model, never a person's name or bio, so no personal data leaves.
"""

import hashlib
import json
import logging
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from radar.config import DomainConfig
from radar.db import Database
from radar.llm.gateway import LlmGateway, LlmRefused
from radar.pipeline.normalise import unmapped_labels

log = logging.getLogger(__name__)

TASK = "classify_label"
PROMPT_VERSION = "label-v1"
SYSTEM = (
    "You map one label from an organisation's website to canonical categories. You receive JSON "
    '{"label": "...", "categories": {"<id>": "<name>", ...}}. Return a JSON object '
    '{"tags": ["<id>", ...]} listing every category id the label clearly belongs to, or [] if none '
    "fits or you are unsure. Use only the given ids. Do not guess."
)


class LabelTags(BaseModel):
    tags: list[str] = Field(default_factory=list)


def taxonomy_fingerprint(domain: DomainConfig) -> str:
    """Changes whenever the taxonomy does, so saved answers for an old taxonomy are ignored."""
    rules = {tag: rule.model_dump() for tag, rule in domain.taxonomy.items()}
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()[:16]


def _key(label: str) -> str:
    return " ".join(label.lower().split())


class LabelClassifier:
    """Looks up (and, when needed, asks the LLM for) tags for labels the keyword rules miss."""

    def __init__(self, db: Database, gateway: LlmGateway | None) -> None:
        self.db = db
        self.gateway = gateway  # None = only use answers already saved, never call the LLM
        self._memo: dict[tuple[str, str], list[str]] = {}
        # Set after the first refusal (no API key, switched off, over budget), so one crawl
        # doesn't write a refusal row to the ledger for every person.
        self._stopped = False

    async def learned_tags(self, labels: list[str], domain: DomainConfig) -> dict[str, list[str]]:
        """Tags for each label in `labels` that no keyword rule matches, keyed by lower-cased label."""
        fingerprint = taxonomy_fingerprint(domain)
        learned: dict[str, list[str]] = {}
        for label in unmapped_labels(labels, domain.taxonomy):
            tags = await self._tags_for(label, domain, fingerprint)
            if tags is not None:
                learned[_key(label)] = tags
        return learned

    async def _tags_for(self, label: str, domain: DomainConfig, fingerprint: str) -> list[str] | None:
        key = _key(label)
        if (key, fingerprint) in self._memo:
            return self._memo[(key, fingerprint)]

        saved = await self.db["label_tags"].find_one({"_id": f"{fingerprint}:{key}"})
        if saved is not None:
            saved_tags: list[str] = saved["tags"]
            self._memo[(key, fingerprint)] = saved_tags
            return saved_tags

        if self.gateway is None or self._stopped:
            return None
        try:
            result = await self.gateway.run(
                task=TASK,
                prompt_version=PROMPT_VERSION,
                system=SYSTEM,
                payload={
                    "label": label,
                    "categories": {tag: rule.label for tag, rule in domain.taxonomy.items()},
                },
                schema=LabelTags,
            )
        except LlmRefused as exc:
            if exc.status != "error":  # a bad answer for one label shouldn't stop the others
                self._stopped = True
            log.info("Not classifying label %r: %s", label, exc.reason)
            return None  # not saved: we'll ask again next time

        # Keep only ids that exist, in case the model invents one.
        tags = [tag for tag in result.output.tags if tag in domain.taxonomy]
        await self.db["label_tags"].replace_one(
            {"_id": f"{fingerprint}:{key}"},
            {
                "label": label,
                "fingerprint": fingerprint,
                "tags": tags,
                "model": result.model,
                "created_at": datetime.now(UTC),
            },
            upsert=True,
        )
        self._memo[(key, fingerprint)] = tags
        return tags
