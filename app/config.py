"""Runtime configuration, loaded from the environment / .env file."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level above this file's package).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


class Settings:
    # Upstream Garage Trip Chores API.
    CHORES_API_BASE: str = os.getenv("CHORES_API_BASE", "https://chores.garage-trip.cz").rstrip("/")
    CHORES_WS_URL: str = os.getenv("CHORES_WS_URL", "wss://chores.garage-trip.cz/ws")
    CHORES_API_KEY: str = os.getenv("CHORES_API_KEY", "")

    # Local persistence.
    DB_PATH: str = os.getenv("DB_PATH", str(PROJECT_ROOT / "chores.db"))

    # Retreat parameters. Little children are not assignable but add to the
    # workload (they eat, they make mess), so they scale head-count chores.
    CHILDREN_COUNT: int = int(os.getenv("CHILDREN_COUNT", "5"))

    @property
    def has_upstream_key(self) -> bool:
        return bool(self.CHORES_API_KEY)


settings = Settings()
