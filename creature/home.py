"""Where the creature keeps its state: authority.json, registry/, queue/ and runs/."""

from __future__ import annotations

import os
from pathlib import Path

ENV = "CREATURE_HOME"


def resolve(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The state root: an explicit path, else $CREATURE_HOME. Never guessed from the code location."""
    raw = explicit if explicit is not None else os.environ.get(ENV)
    if not raw:
        raise RuntimeError(f"no creature home: pass one or set ${ENV}")
    root = Path(raw).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"creature home {root} is not a directory")
    return root
