from pathlib import Path

from nailong_agent.app import main
from nailong_agent.config import NailongSettings


def test_settings_derive_desktop_paths_from_environment(monkeypatch, tmp_path: Path) -> None:
    data_dir = tmp_path / "pet-data"
    monkeypatch.setenv("NAILONG_DATA_DIR", str(data_dir))
    monkeypatch.setenv("NAILONG_ANALYSIS_URL", "http://127.0.0.1:18000")
    monkeypatch.setenv("NAILONG_DEEPSEEK_MODEL", "deepseek-chat")
    monkeypatch.setenv("NAILONG_MAXIMUM_POPUPS_PER_DAY", "8")

    settings = NailongSettings.from_env()

    assert settings.data_dir == data_dir
    assert settings.lock_path == data_dir / "nailong-agent.lock"
    assert settings.privacy_database == data_dir / "nailong_privacy.sqlite"
    assert settings.notification_database == data_dir / "nailong_notifications.sqlite"
    assert settings.analysis_url == "http://127.0.0.1:18000"
    assert settings.deepseek_model == "deepseek-chat"
    assert settings.maximum_popups_per_day == 8


def test_explicit_path_override_wins_over_derived_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "pet-data"
    notification_database = tmp_path / "custom.sqlite"

    settings = NailongSettings(data_dir=data_dir).with_overrides(
        notification_database=notification_database
    )

    assert settings.notification_database == notification_database
    assert settings.privacy_database == data_dir / "nailong_privacy.sqlite"


def test_runtime_preference_overrides_include_only_configured_values() -> None:
    settings = NailongSettings(
        maximum_popups_per_day=8,
        minimum_cooldown_seconds=60,
    )

    assert settings.notification_preference_overrides == {
        "maximum_popups_per_day": 8,
        "minimum_cooldown_seconds": 60,
    }


def test_settings_load_activity_listener_switch(monkeypatch) -> None:
    monkeypatch.setenv("NAILONG_ACTIVITY_LISTENER_ENABLED", "false")

    settings = NailongSettings.from_env()

    assert settings.activity_listener_enabled is False


def test_headless_entrypoint_uses_data_directory_for_lock_and_privacy_store(tmp_path: Path) -> None:
    data_dir = tmp_path / "pet-data"

    assert main(["--headless", "--data-dir", str(data_dir)]) == 0
    assert (data_dir / "nailong-agent.lock").exists()
    assert (data_dir / "nailong_privacy.sqlite").exists()
