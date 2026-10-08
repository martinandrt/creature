"""Keys, read at run time from the file named by $CREATURE_SECRETS. Never stored, logged or passed on."""

from __future__ import annotations

import os
from pathlib import Path

ENV = "CREATURE_SECRETS"


def get(name: str) -> str:
    """The value of `name` from the secrets file (KEY=VALUE lines, optional `export` and quotes)."""
    path = os.environ.get(ENV)
    if not path:
        raise RuntimeError(f"${ENV} is not set: point it at a KEY=VALUE file")
    for line in Path(path).expanduser().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value:
                return value
    raise RuntimeError(f"{name} is not in the secrets file")  # the value never appears in errors
