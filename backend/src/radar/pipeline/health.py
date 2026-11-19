"""Is this crawl trustworthy? A broken scraper must never look like a mass exodus."""

from typing import Literal

DiscoveryHealth = Literal["ok", "quarantined", "broken"]

MAX_DROP = 0.15


def assess_discovery(previous_count: int | None, current_count: int) -> DiscoveryHealth:
    """Compare the number of profile URLs discovered now against the last good run.

    - `broken`: nothing discovered at all (site redesign, sitemap moved, blocked).
    - `quarantined`: the list shrank by more than 15% - far more than real churn in a week.
      New URLs are still processed, but nobody is marked as departed from this run.
    """
    if current_count == 0:
        return "broken"
    if previous_count and current_count < previous_count * (1 - MAX_DROP):
        return "quarantined"
    return "ok"
