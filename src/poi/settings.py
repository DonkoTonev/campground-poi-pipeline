"""Typed runtime configuration.

Secrets and environment-specific values come from env vars / `.env` (pydantic-settings).
Domain rules (regions, matching weights, survivorship, prices) live in YAML under `config/`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        env_ignore_empty=True,  # `RIDB_API_KEY=` means "not configured", not "empty key"
    )

    database_url: str = "postgresql+psycopg://poi:poi@localhost:5433/poi"
    elasticsearch_url: str = "http://localhost:9200"

    raw_store: Literal["local", "s3"] = "local"
    raw_store_path: Path = PROJECT_ROOT / "data" / "raw"
    raw_store_s3_bucket: str | None = None

    config_dir: Path = PROJECT_ROOT / "config"
    region: str = "sierra"

    # Crawler / API client identity. The contact URL is required for any outbound request.
    contact_url: str | None = None

    overpass_url: str = "https://overpass-api.de/api/interpreter"
    ridb_base_url: str = "https://ridb.recreation.gov/api/v1"
    nps_base_url: str = "https://developer.nps.gov/api/v1"
    # Test-only escape hatch for crawling a local fixture server; SSRF screening otherwise.
    crawl_allow_private_hosts: bool = False

    anthropic_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    ridb_api_key: SecretStr | None = None
    nps_api_key: SecretStr | None = None

    log_level: str = "INFO"
    log_json: bool = False

    @property
    def user_agent(self) -> str:
        """Honest, identifiable User-Agent. Refuses to build one without a contact URL."""
        if not self.contact_url:
            raise RuntimeError("POI contact URL missing: set CONTACT_URL in .env")
        return f"campground-poi-pipeline/0.1 (+{self.contact_url})"


class Region(BaseModel):
    name: str
    description: str = ""
    # (south, west, north, east) in WGS84 degrees, Overpass order.
    bbox: tuple[float, float, float, float]
    states: list[str] = Field(default_factory=list)
    nps_park_codes: list[str] = Field(default_factory=list)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_yaml(name: str, settings: Settings | None = None) -> dict[str, object]:
    settings = settings or get_settings()
    with (settings.config_dir / name).open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{name}: expected a mapping at top level")
    return data


def load_region(name: str | None = None, settings: Settings | None = None) -> Region:
    settings = settings or get_settings()
    name = name or settings.region
    regions = load_yaml("regions.yaml", settings)
    if name not in regions:
        raise KeyError(f"unknown region {name!r}; known: {sorted(regions)}")
    raw = regions[name]
    if not isinstance(raw, dict):
        raise ValueError(f"region {name!r} must be a mapping")
    return Region(name=name, **raw)
