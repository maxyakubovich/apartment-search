"""Config and secret loading.

Config lives in config.yaml (committed, public). Secrets come from the
environment — GitHub Secrets in CI, a gitignored .env locally.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"
_REPO_ROOT = Path(__file__).parent.parent


def load_config(path: Path | None = None) -> dict[str, Any]:
    with open(path or CONFIG_PATH) as f:
        return yaml.safe_load(f)


def load_dotenv(path: Path | None = None) -> None:
    """Minimal .env reader so local runs work without extra dependencies.

    Never overrides an already-set variable, so CI secrets always win.
    """
    env_path = path or (_REPO_ROOT / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def require(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required secret {name}. Set it in .env locally "
            f"or as a repository Secret in GitHub."
        )
    return value


def optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()
