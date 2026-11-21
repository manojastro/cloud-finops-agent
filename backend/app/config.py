from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    app_name: str = "Cloud FinOps & Change-Risk Optimization Agent"
    environment: str = os.getenv("APP_ENV", "local")
    database_path: Path = Path(os.getenv("DATABASE_PATH", str(PROJECT_DIR / "data" / "finops.db")))
    llm_mode: str = os.getenv("LLM_MODE", "mock")
    allowed_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://localhost:8102").split(",")
        if origin.strip()
    )
    rate_limit_per_minute: int = int(os.getenv("RATE_LIMIT_PER_MINUTE", "120"))


settings = Settings()
