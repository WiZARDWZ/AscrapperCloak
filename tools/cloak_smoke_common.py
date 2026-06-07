from __future__ import annotations

import os
from datetime import datetime

import config


def add_profile_args(parser) -> None:
    parser.add_argument("--profile-dir", default=None)
    parser.add_argument("--fresh-profile", action="store_true")


def resolve_profile_dir(args, out_dir: str, prefix: str) -> str:
    if getattr(args, "profile_dir", None):
        profile_dir = args.profile_dir
    elif getattr(args, "fresh_profile", False):
        profile_dir = os.path.join(out_dir, f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    else:
        profile_dir = config.get_effective_browser_profile_dir()
    return os.path.abspath(profile_dir)


def apply_profile_dir(profile_dir: str, module2: bool = False) -> str:
    effective = os.path.abspath(profile_dir)
    config.CHROME_PROFILE_DIR = effective
    config.CLOAK_PROFILE_DIR = effective
    config.BROWSER_USE_RUNTIME_PROFILE_STATE = False
    if module2:
        config.MODULE2_PROFILE_BASE_DIR = effective
    print(f"effective_profile_dir={effective}")
    return effective


def add_checkpoint_args(parser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--fresh-checkpoint", action="store_true", default=False)
    group.add_argument("--resume", action="store_true")


def smoke_checkpoint_path(out_dir: str, prefix: str) -> str:
    return os.path.abspath(os.path.join(out_dir, f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"))
