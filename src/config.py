#!/usr/bin/env python3
"""
Runtime configuration, read once at import.

Every setting comes from the environment, optionally seeded from a .env file.
Missing required values fail here with a readable message rather than as a
KeyError three call frames deep during startup.
"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).parent.parent

load_dotenv(PROJECT_ROOT / ".env")


class ConfigError(RuntimeError):
    pass


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in, "
            f"or export {name} in your shell."
        )
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Config:
    supabase_url: str
    supabase_service_key: str

    # Public (pk.) Mapbox token, served to the browser via /api/config.
    # Empty is allowed — the frontend reports a clear error instead of a blank map.
    mapbox_token: str

    static_dir: Path
    poll_interval: float

    @classmethod
    def load(cls) -> "Config":
        return cls(
            supabase_url=_required("SUPABASE_URL"),
            supabase_service_key=_required("SUPABASE_SERVICE_KEY"),
            mapbox_token=_optional("MAPBOX_TOKEN"),
            static_dir=PROJECT_ROOT / "static",
            poll_interval=float(_optional("POLL_INTERVAL", "15")),
        )


config = Config.load()
