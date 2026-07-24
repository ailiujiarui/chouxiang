from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class NailongSettings(BaseModel):
    """Desktop-only configuration and derived local storage paths."""

    data_dir: Path = Path(".runs")
    analysis_url: str | None = None
    deepseek_model: str | None = None
    maximum_popups_per_day: int | None = Field(default=None, ge=0)
    minimum_cooldown_seconds: int | None = Field(default=None, ge=0)
    maximum_cooldown_seconds: int | None = Field(default=None, ge=0)
    lock_path_override: Path | None = None
    privacy_database_override: Path | None = None
    notification_database_override: Path | None = None

    @classmethod
    def from_env(cls) -> "NailongSettings":
        return cls(
            data_dir=Path(os.getenv("NAILONG_DATA_DIR", ".runs")),
            analysis_url=os.getenv("NAILONG_ANALYSIS_URL"),
            deepseek_model=os.getenv("NAILONG_DEEPSEEK_MODEL"),
            maximum_popups_per_day=_optional_int("NAILONG_MAXIMUM_POPUPS_PER_DAY"),
            minimum_cooldown_seconds=_optional_int("NAILONG_MINIMUM_COOLDOWN_SECONDS"),
            maximum_cooldown_seconds=_optional_int("NAILONG_MAXIMUM_COOLDOWN_SECONDS"),
        )

    def with_overrides(
        self,
        *,
        data_dir: Path | None = None,
        analysis_url: str | None = None,
        deepseek_model: str | None = None,
        maximum_popups_per_day: int | None = None,
        minimum_cooldown_seconds: int | None = None,
        maximum_cooldown_seconds: int | None = None,
        lock_path: Path | None = None,
        privacy_database: Path | None = None,
        notification_database: Path | None = None,
    ) -> "NailongSettings":
        updates = {
            "data_dir": data_dir,
            "analysis_url": analysis_url,
            "deepseek_model": deepseek_model,
            "maximum_popups_per_day": maximum_popups_per_day,
            "minimum_cooldown_seconds": minimum_cooldown_seconds,
            "maximum_cooldown_seconds": maximum_cooldown_seconds,
            "lock_path_override": lock_path,
            "privacy_database_override": privacy_database,
            "notification_database_override": notification_database,
        }
        return self.model_copy(update={name: value for name, value in updates.items() if value is not None})

    @property
    def lock_path(self) -> Path:
        return self.lock_path_override or self.data_dir / "nailong-agent.lock"

    @property
    def privacy_database(self) -> Path:
        return self.privacy_database_override or self.data_dir / "nailong_privacy.sqlite"

    @property
    def notification_database(self) -> Path:
        return self.notification_database_override or self.data_dir / "nailong_notifications.sqlite"

    @property
    def notification_preference_overrides(self) -> dict[str, int]:
        values = {
            "maximum_popups_per_day": self.maximum_popups_per_day,
            "minimum_cooldown_seconds": self.minimum_cooldown_seconds,
            "maximum_cooldown_seconds": self.maximum_cooldown_seconds,
        }
        return {name: value for name, value in values.items() if value is not None}


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    return int(value) if value else None
