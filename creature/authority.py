"""The creature's authority: a human-edited file plus the code that enforces it.

The fingerprint covers authority.json and every module that enforces or proves it: this checker, the
ledger, the gate, the sandbox, the broker, the model call (spend caps), the forge loop (attempt caps)
and the registry (install only after passing tests). A run takes it at the start and at the end; any
difference means authority changed while the creature ran. A module that starts enforcing a rule must
be added to ENFORCERS.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

AUTHORITY_FILE = "authority.json"
ENFORCERS = (
    "authority.py",
    "ledger.py",
    "gate.py",
    "sandbox.py",
    "broker.py",
    "llm.py",
    "forge.py",
    "registry.py",
)
MISSING = "missing"


@dataclass(frozen=True)
class Fingerprint:
    digest: str
    parts: dict[str, str]  # file name -> sha256 of its bytes, or "missing"

    @property
    def short(self) -> str:
        return self.digest[:12]


def fingerprint(home: str | Path, code_root: Traversable | Path | None = None) -> Fingerprint:
    """sha256 over the per-file hashes of authority.json and the enforcers, in a fixed order."""
    code = code_root if code_root is not None else resources.files("creature")
    authority_path = Path(home) / AUTHORITY_FILE
    if not authority_path.is_file():
        # without the file "unchanged" would be vacuously true
        raise FileNotFoundError(f"no {AUTHORITY_FILE} in {home}")
    parts = {AUTHORITY_FILE: _file_digest(authority_path)}
    for name in ENFORCERS:
        parts[name] = _file_digest(code / name)
    combined = hashlib.sha256()
    for name, digest in parts.items():
        combined.update(f"{name}\0{digest}\n".encode())
    return Fingerprint(combined.hexdigest(), parts)


def changed(before: Fingerprint, after: Fingerprint) -> list[str]:
    """Names of the files whose hash differs between two fingerprints."""
    return [name for name, digest in before.parts.items() if after.parts.get(name) != digest]


def _file_digest(path: Traversable | Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else MISSING
