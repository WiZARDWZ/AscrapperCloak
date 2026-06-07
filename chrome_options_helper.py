import json
import os
import re

import config
from cloak_browser_helper import build_cloak_driver, cleanup_cloak_driver


def _looks_like_windows_path(path: str) -> bool:
    text = str(path or "").strip()
    return bool(re.match(r"^[A-Za-z]:[\\/]", text) or "\\Users\\" in text or "\\PycharmProjects\\" in text)


def _profile_path_is_usable(path: str | None) -> bool:
    text = str(path or "").strip()
    if not text:
        return False
    if os.name != "nt" and _looks_like_windows_path(text):
        return False
    return True


def _read_runtime_profile_override() -> str | None:
    if not (config.BROWSER_USE_RUNTIME_PROFILE_STATE and os.path.isfile(config.BROWSER_PROFILE_STATE_PATH)):
        return None
    try:
        with open(config.BROWSER_PROFILE_STATE_PATH, "r", encoding="utf-8") as f:
            state = json.load(f)
        runtime_override = (state.get("current_profile_dir") or "").strip() or None
    except Exception:
        return None
    if runtime_override and not _profile_path_is_usable(runtime_override):
        print(f"Ignoring incompatible browser profile state path: {runtime_override}")
        return None
    return runtime_override


def _effective_profile_override(profile_dir_override: str | None) -> str | None:
    if profile_dir_override:
        if _profile_path_is_usable(profile_dir_override):
            return profile_dir_override
        print(f"Ignoring incompatible browser profile override: {profile_dir_override}")
        return None
    return _read_runtime_profile_override()


def build_chrome_driver(profile_dir_override: str | None = None):
    """Backward-compatible entrypoint now backed by CloakBrowser."""
    effective_override = _effective_profile_override(profile_dir_override)
    return build_cloak_driver(profile_dir_override=effective_override)


def cleanup_chrome_driver(driver) -> None:
    cleanup_cloak_driver(driver)

