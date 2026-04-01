from __future__ import annotations

import os
from pathlib import Path


def _local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home())))


def preferred_state_root() -> Path:
    override = os.environ.get("FETCHXH_HOME")
    if override:
        return Path(override).expanduser()
    return _local_appdata() / "fetchxh"


def legacy_state_root() -> Path:
    return _local_appdata() / "fetchx"


def state_roots() -> list[Path]:
    roots: list[Path] = []
    for candidate in (preferred_state_root(), legacy_state_root()):
        if candidate not in roots:
            roots.append(candidate)
    return roots


def first_existing_path(*parts: str) -> Path | None:
    for root in state_roots():
        path = root.joinpath(*parts)
        if path.exists():
            return path
    return None


def preferred_x_state_path() -> Path:
    override = os.environ.get("FETCHXH_X_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return preferred_state_root() / "x_state.json"

