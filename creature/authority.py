"""The creature's authority: a human-edited file plus the code that enforces it.

authority.json holds every cap and limit; the spine reads them from here, never from constants.
The fingerprint covers authority.json, the workshop image ID and every module that enforces or proves
the rules: this checker, the ledger, the gate, the workshop, the verdict, the model call (spend caps),
the forge loop (attempt caps) and the registry (install only after passing checks). A run takes it at
the start and at the end; any difference means authority changed while the creature ran. A module
that starts enforcing a rule must be added to ENFORCERS.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Any

from creature.workshop import Limits

AUTHORITY_FILE = "authority.json"
IMAGE_PART = "workshop image"
ENFORCERS = (
    "authority.py",
    "ledger.py",
    "gate.py",
    "workshop.py",
    "verdict.py",
    "llm.py",
    "forge.py",
    "registry.py",
)
MISSING = "missing"


class AuthorityError(ValueError):
    """authority.json is missing a value or holds one the spine cannot honour."""


@dataclass(frozen=True)
class Caps:
    run_budget_usd: float
    call_reserve_usd: float
    forge_attempts: int
    new_skills_per_run: int
    step_cap_usd: dict[str, float]


@dataclass(frozen=True)
class Authority:
    version: int
    caps: Caps
    workshop: Limits
    image: str
    models: dict[str, str]  # step name -> model; "default" for the rest
    refuse: tuple[str, ...]
    ask: tuple[str, ...]


def load(home: str | Path) -> Authority:
    """Read and check authority.json. Anything missing or out of range stops the run."""
    path = Path(home) / AUTHORITY_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise AuthorityError(f"no {AUTHORITY_FILE} in {home}") from None
    except json.JSONDecodeError as error:
        raise AuthorityError(f"{AUTHORITY_FILE} is not JSON: {error}") from None
    caps = _section(raw, "caps")
    shop = _section(raw, "workshop")
    if shop.get("network") is not False:
        raise AuthorityError("workshop.network must be false: the spine cannot give skills a network")
    steps = caps.get("step_cap_usd")
    if not isinstance(steps, dict) or not steps:
        raise AuthorityError("caps.step_cap_usd must map step names to dollar caps")
    image = shop.get("image")
    if not isinstance(image, str) or not image:
        raise AuthorityError("workshop.image must name the workshop image")
    models = _section(raw, "models")
    if not isinstance(models.get("default"), str) or not all(
        isinstance(v, str) and v for v in models.values()
    ):
        raise AuthorityError("models must map step names to model names, with a default")
    return Authority(
        version=_number(raw, "version", integer=True, minimum=1),
        caps=Caps(
            run_budget_usd=_number(caps, "run_budget_usd", minimum=0),
            call_reserve_usd=_number(caps, "call_reserve_usd", positive=True),
            forge_attempts=_number(caps, "forge_attempts", integer=True, minimum=1),
            new_skills_per_run=_number(caps, "new_skills_per_run", integer=True, minimum=0),
            step_cap_usd={str(step): _number(steps, step, positive=True) for step in steps},
        ),
        workshop=Limits(
            timeout_s=_number(shop, "timeout_s", positive=True),
            memory_mb=_number(shop, "memory_mb", integer=True, minimum=64),
            cpus=_number(shop, "cpus", positive=True),
            pids=_number(shop, "pids", integer=True, minimum=1),
            work_mb=_number(shop, "work_mb", integer=True, minimum=1),
            tmp_mb=_number(shop, "tmp_mb", integer=True, minimum=1),
            output_mb=_number(shop, "output_mb", integer=True, minimum=1),
        ),
        image=image,
        models=dict(models),
        refuse=_strings(raw, "refuse"),
        ask=_strings(raw, "ask"),
    )


def _section(raw: Any, key: str) -> dict[str, Any]:
    value = raw.get(key) if isinstance(raw, dict) else None
    if not isinstance(value, dict):
        raise AuthorityError(f"{AUTHORITY_FILE} has no {key!r} section")
    return value


def _number(
    section: dict[str, Any],
    key: str,
    *,
    integer: bool = False,
    positive: bool = False,
    minimum: float | None = None,
) -> Any:
    value = section.get(key)
    kinds = int if integer else int | float
    # bool is an int in Python, and NaN or inf would switch a cap off
    if isinstance(value, bool) or not isinstance(value, kinds) or not math.isfinite(value):
        raise AuthorityError(f"{key} must be a finite {'integer' if integer else 'number'}, got {value!r}")
    if positive and value <= 0:
        raise AuthorityError(f"{key} must be > 0, got {value!r}")
    if minimum is not None and value < minimum:
        raise AuthorityError(f"{key} must be >= {minimum}, got {value!r}")
    return value


def _strings(raw: dict[str, Any], key: str) -> tuple[str, ...]:
    value = raw.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise AuthorityError(f"{key} must be a list of non-empty strings")
    return tuple(value)


@dataclass(frozen=True)
class Fingerprint:
    digest: str
    parts: dict[str, str]  # part name -> sha256 of a file, the workshop image ID, or "missing"

    @property
    def short(self) -> str:
        return self.digest[:12]


def fingerprint(
    home: str | Path, code_root: Traversable | Path | None = None, *, image_id: str
) -> Fingerprint:
    """sha256 over authority.json, the workshop image ID and the enforcers, in a fixed order."""
    code = code_root if code_root is not None else resources.files("creature")
    authority_path = Path(home) / AUTHORITY_FILE
    if not authority_path.is_file():
        # without the file "unchanged" would be vacuously true
        raise FileNotFoundError(f"no {AUTHORITY_FILE} in {home}")
    if not image_id:
        raise ValueError("the fingerprint needs the workshop image ID")
    parts = {AUTHORITY_FILE: _file_digest(authority_path), IMAGE_PART: image_id}
    for name in ENFORCERS:
        parts[name] = _file_digest(code / name)
    combined = hashlib.sha256()
    for name, digest in parts.items():
        combined.update(f"{name}\0{digest}\n".encode())
    return Fingerprint(combined.hexdigest(), parts)


def changed(before: Fingerprint, after: Fingerprint) -> list[str]:
    """Names of the parts that differ between two fingerprints."""
    return [name for name, digest in before.parts.items() if after.parts.get(name) != digest]


def _file_digest(path: Traversable | Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else MISSING
