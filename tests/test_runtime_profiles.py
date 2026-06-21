from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest import mock

import config
import cloak_browser_helper


def _reload_config(profile: str, **overrides):
    env = {
        "RUNTIME_PROFILE": profile,
        "LOAD_DOTENV": "0",
        **{key: str(value) for key, value in overrides.items()},
    }
    return mock.patch.dict(os.environ, env, clear=True)


def test_windows_dev_profile_resolves_visible_project_local_runtime():
    with _reload_config("windows_dev"):
        reloaded = importlib.reload(config)
        root = Path(reloaded.PROJECT_ROOT).resolve()
        assert reloaded.RUNTIME_PROFILE == "windows_dev"
        assert reloaded.HEADLESS is False
        assert reloaded.CLOAK_HEADLESS is False
        assert reloaded.BROWSER_ENGINE == "cloak"
        assert reloaded.CLOAK_VIEWPORT_WIDTH == 1365
        assert reloaded.CLOAK_VIEWPORT_HEIGHT == 768
        for value in (
            reloaded.RUNTIME_DIR,
            reloaded.OUTPUT_DIR,
            reloaded.LOG_DIR,
            reloaded.CLOAK_PROFILE_DIR,
        ):
            assert Path(value).resolve().is_relative_to(root)
        assert Path(reloaded.CLOAK_PROFILE_DIR).name == "rea-profile"
    importlib.reload(config)


def test_windows_dev_paths_can_be_created_and_written():
    profile = config.get_runtime_profile_config("windows_dev")
    root = Path(config.PROJECT_ROOT).resolve()
    for key in ("RUNTIME_DIR", "OUTPUT_DIR", "LOG_DIR", "CLOAK_PROFILE_DIR"):
        path = Path(profile[key]).resolve()
        assert path.is_relative_to(root)
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".runtime-profile-write-test"
        probe.write_text("ok", encoding="utf-8")
        assert probe.read_text(encoding="utf-8") == "ok"
        probe.unlink()


def test_ubuntu_prod_keeps_env_overrides_and_odbc18():
    with _reload_config(
        "ubuntu_prod",
        DB_HOST="sql.example.test",
        DB_NAME="AScrapperProd",
        OUTPUT_DIR="custom-output",
        CLOAK_PROFILE_DIR="rea_profile",
        HEADLESS="0",
    ):
        reloaded = importlib.reload(config)
        assert reloaded.RUNTIME_PROFILE == "ubuntu_prod"
        assert reloaded.is_production() is True
        assert reloaded.DB_DRIVER == "ODBC Driver 18 for SQL Server"
        assert reloaded.DB_HOST == "sql.example.test"
        assert reloaded.DB_NAME == "AScrapperProd"
        assert reloaded.OUTPUT_DIR == "custom-output"
        assert reloaded.CLOAK_PROFILE_DIR == "rea_profile"
        assert reloaded.BROWSER_PROFILE_BASE_DIR == "rea_profile"
        assert reloaded.HEADLESS is False
        with mock.patch.object(reloaded.os, "name", "posix"):
            assert reloaded.get_profile_settings("normal")["USE_XVFB"] is True
    importlib.reload(config)


def test_profile_defaults_do_not_embed_foreign_absolute_paths():
    windows_profile = config.get_runtime_profile_config("windows_dev")
    ubuntu_profile = config.get_runtime_profile_config("ubuntu_prod")
    assert all("/opt/" not in str(value) for value in windows_profile.values())
    for profile in (windows_profile, ubuntu_profile):
        for key in ("RUNTIME_DIR", "OUTPUT_DIR", "LOG_DIR", "CLOAK_PROFILE_DIR"):
            assert Path(profile[key]).resolve().is_relative_to(Path(config.PROJECT_ROOT).resolve())


def test_windows_headed_mode_does_not_require_display(tmp_path):
    with mock.patch.object(cloak_browser_helper.os, "name", "nt"), mock.patch.dict(
        os.environ, {}, clear=True
    ):
        cloak_browser_helper.validate_cloak_runtime(
            str(tmp_path),
            {"headless": False},
        )


def test_linux_headed_mode_without_display_fails_clearly(tmp_path):
    with mock.patch.object(cloak_browser_helper.os, "name", "posix"), mock.patch.dict(
        os.environ, {}, clear=True
    ):
        try:
            cloak_browser_helper.validate_cloak_runtime(
                str(tmp_path),
                {"headless": False},
            )
        except cloak_browser_helper.BrowserConfigurationError as exc:
            assert "DISPLAY/Xvfb" in str(exc)
        else:
            raise AssertionError("Linux headed mode without DISPLAY must fail")


def test_linux_headless_mode_does_not_require_display(tmp_path):
    with mock.patch.object(cloak_browser_helper.os, "name", "posix"), mock.patch.dict(
        os.environ, {}, clear=True
    ):
        cloak_browser_helper.validate_cloak_runtime(
            str(tmp_path),
            {"headless": True},
        )
