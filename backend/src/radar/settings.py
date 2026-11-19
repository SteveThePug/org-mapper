"""Process configuration, read from environment variables (and `.env` when present).

Everything secret or deployment-specific lives here; everything about *what* we scrape
(organisations, the domain profile) lives in the YAML files under `config/`.
"""

from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# backend/src/radar/settings.py -> repo root is three parents up from this package.
_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    mongo_uri: str = "mongodb://localhost:27017"
    mongo_db: str = "radar"
    config_dir: Path = _REPO_ROOT / "config"
    # Which config/domains/*.yaml profile to track (legal-partners, football, ...).
    radar_domain: str = "legal-partners"
    data_dir: Path = _REPO_ROOT / "data"

    # Scraping etiquette: identify ourselves and stay slow.
    scraper_user_agent: str = "PartnerRadarBot/0.1 (+set SCRAPER_USER_AGENT to include a contact address)"
    scraper_min_delay_s: float = 3.0
    scraper_timeout_s: float = 20.0
    # Off in production: never fetch localhost / private networks (see fetcher.PrivateAddress).
    # Turn on only to test against a site served on your own machine or LAN.
    scraper_allow_private_hosts: bool = False
    worker_lanes: int = 4
    discovery_interval_h: float = 24.0
    refresh_in_scope_days: float = 7.0
    refresh_out_of_scope_days: float = 30.0

    # LLM via OpenRouter (OpenAI-compatible API). Empty key = AI features off.
    openrouter_api_key: str = Field(
        default="", validation_alias=AliasChoices("OPENROUTER_API_KEY", "OPENROUTER_KEY")
    )
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_model_brief: str = "nvidia/nemotron-3.5-super-vl:free"
    llm_model_cheap: str = "google/gemma-4-31b-it:free"
    llm_fallback_models: Annotated[list[str], NoDecode] = ["google/gemma-4-31b-it:free"]
    llm_daily_budget_usd: float = 1.0
    # When true, OpenRouter only routes to providers that don't store/train on prompts.
    # Many free endpoints *do* log prompts, so this is off by default for the demo; we only
    # send public professional data either way.
    llm_deny_data_collection: bool = False

    session_secret: str = "dev-insecure-change-me"  # noqa: S105 - dev default, warned about at startup
    session_https_only: bool = False
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""
    cors_origins: Annotated[list[str], NoDecode] = ["http://localhost:5173"]

    @field_validator("llm_fallback_models", "cors_origins", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def llm_enabled(self) -> bool:
        return bool(self.openrouter_api_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()
