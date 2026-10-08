"""Model calls for the spine: isolated, schema-shaped, budget-capped and logged.

Every call runs the pinned Claude Code CLI with its own system prompt, no tools, no MCP servers and no
user settings, from an empty directory, so the model sees nothing but what the spine sends.

Caps (all from authority.json): before each call the run's remaining budget must cover a reserve (the
cost of a large call at fallback-model prices); otherwise the call is refused and the model is never
reached. The CLI checks --max-budget-usd only after a turn, so it is a second net: a call over its cap
still costs, then returns no result. A run can therefore overshoot its budget by at most one call.
"""

from __future__ import annotations

import base64
import json
import math
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from creature.ledger import Ledger

CLI_PACKAGE = "@anthropic-ai/claude-code@2.1.294"
MODEL = "claude-haiku-5-5"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"
# the CLI child gets only what it needs to run and log in; everything else in the shell stays out
ENV_KEEP = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM",
    "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE",
)  # fmt: skip
INPUT_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
MAX_IMAGE_BYTES = 5_000_000


@dataclass(frozen=True)
class ModelCall:
    step: str  # spine step that asks: "planner", "examiner", "forge", "perceive", "skill:<name>"
    model: str
    system: str
    prompt: str
    schema: dict[str, Any]
    max_usd: float
    images: tuple[tuple[str, Path], ...] = ()  # (label, png or jpeg file), shown before the prompt


@dataclass(frozen=True)
class ModelReply:
    data: dict[str, Any]  # structured_output from the CLI
    cost_usd: float  # total_cost_usd from the CLI, list-price equivalent
    model: str = MODEL
    input_tokens: int = 0
    output_tokens: int = 0


class Transport(Protocol):
    """One isolated model call. Caps, ledger events and retries live above it, in Model."""

    def complete(self, call: ModelCall) -> ModelReply: ...


class ModelError(Exception):
    def __init__(self, message: str, cost_usd: float = 0.0) -> None:
        super().__init__(message)
        self.cost_usd = cost_usd


class BudgetRefused(ModelError):
    """The call was not made: the run's remaining budget does not cover the reserve."""


class LimitReached(BudgetRefused):
    """The account's usage limit: no call can succeed until it resets, so the run stops, never retries."""


LIMIT_SIGNS = ("hit your session limit", "hit your weekly limit", "hit your usage limit")


class Model:
    """The only way the spine reaches a model. Enforces the run budget and logs every call."""

    def __init__(
        self,
        transport: Transport,
        ledger: Ledger,
        *,
        budget_usd: float,
        reserve_usd: float,
        model: str = MODEL,
    ) -> None:
        # NaN compares False everywhere and would switch the cap off; inf is no budget at all
        if not (math.isfinite(budget_usd) and budget_usd >= 0):
            raise ValueError(f"budget_usd must be a finite number >= 0, got {budget_usd!r}")
        if not (math.isfinite(reserve_usd) and reserve_usd > 0):
            raise ValueError(f"reserve_usd must be a finite number > 0, got {reserve_usd!r}")
        self.transport = transport
        self.ledger = ledger
        self.budget_usd = budget_usd
        self.reserve_usd = reserve_usd
        self.limited = ""  # set by the first usage-limit error: later calls are refused without trying
        self.model = model

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - self.ledger.spent_usd

    def ask(
        self,
        step: str,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        *,
        cap_usd: float,
        images: tuple[tuple[str, Path], ...] = (),
        model: str | None = None,
    ) -> dict[str, Any]:
        """Structured answer for one step, or ModelError. A call is logged with its cost or not made."""
        if not (math.isfinite(cap_usd) and cap_usd > 0):
            raise ValueError(f"{step}: cap_usd must be a finite number > 0, got {cap_usd!r}")
        images = tuple((label, Path(path)) for label, path in images)
        for _, path in images:
            _check_image(path)
        if self.limited:
            self.ledger.record("model_refused", step=step, reason="usage limit")
            raise LimitReached(f"{step}: {self.limited}")
        remaining = self.remaining_usd
        if remaining < self.reserve_usd:
            self.ledger.record("model_refused", step=step, reason="budget", remaining_usd=round(remaining, 6))
            raise BudgetRefused(f"{step}: ${remaining:.4f} left, a call needs a ${self.reserve_usd} reserve")
        max_usd = round(min(cap_usd, remaining), 6)
        call = ModelCall(step, model or self.model, system, prompt, schema, max_usd=max_usd, images=images)
        try:
            reply = self.transport.complete(call)
        except ModelError as error:
            self.ledger.record(
                "model_call", step=step, model=call.model, ok=False, error=str(error)[:500],
                max_usd=call.max_usd, cost_usd=error.cost_usd,
            )  # fmt: skip
            if isinstance(error, LimitReached):
                self.limited = str(error)  # every later call would hit the same wall
            raise
        problem = _shape_problem(reply.data, schema)
        self.ledger.record(
            "model_call", step=step, model=reply.model, ok=problem is None, max_usd=call.max_usd,
            cost_usd=reply.cost_usd, input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
            **({"error": problem} if problem else {}),
        )  # fmt: skip
        if problem:
            raise ModelError(f"{step}: {problem}")
        return reply.data


class ClaudeCLI:
    """Transport over the pinned Claude Code CLI (`claude -p`)."""

    def __init__(self, binary: str | None = None, *, timeout_s: float = 180.0) -> None:
        self._binary = binary or os.environ.get("CREATURE_CLAUDE")
        self.timeout_s = timeout_s

    @property
    def binary(self) -> str:
        # resolved on first use: a run that never calls the model never touches the CLI
        if self._binary is None:
            self._binary = resolve_binary()
        return self._binary

    def command(self, call: ModelCall) -> list[str]:
        # the prompt goes over stdin as a stream-json message: never read as a flag, and it can carry images
        return [
            self.binary, "-p",
            "--model", call.model, "--fallback-model", FALLBACK_MODEL,
            "--system-prompt", call.system,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
            "--json-schema", json.dumps(call.schema),
            "--max-budget-usd", f"{call.max_usd:.6f}",
        ]  # fmt: skip

    def complete(self, call: ModelCall) -> ModelReply:
        env = {key: value for key, value in os.environ.items() if key in ENV_KEEP}
        with tempfile.TemporaryDirectory(prefix="creature-model-") as empty:
            try:
                proc = subprocess.run(
                    self.command(call), input=message(call), capture_output=True, text=True,
                    cwd=empty, env=env, timeout=self.timeout_s,
                )  # fmt: skip
            except subprocess.TimeoutExpired:
                # the cost of a killed call is unknown: count the whole cap
                raise ModelError(f"timeout after {self.timeout_s}s", cost_usd=call.max_usd) from None
        return parse(proc.stdout, call, stderr=proc.stderr)


def message(call: ModelCall) -> str:
    """The one user message, as a stream-json line: each image after its label, then the prompt."""
    content: list[dict[str, Any]] = []
    for label, path in call.images:
        data = base64.b64encode(path.read_bytes()).decode()
        content.append({"type": "text", "text": f"{label}:"})
        content.append(
            {"type": "image", "source": {"type": "base64", "media_type": _media_type(path), "data": data}}
        )
    content.append({"type": "text", "text": call.prompt})
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}}) + "\n"


def parse(stdout: str, call: ModelCall, *, stderr: str = "") -> ModelReply:
    """ModelReply from the CLI's result line, or ModelError carrying whatever the call cost."""
    result = _result_line(stdout)
    if result is None:
        raise ModelError(f"CLI gave no result: {(stdout or stderr).strip()[-300:]}")
    cost = _cost(result.get("total_cost_usd"), call)
    if result.get("is_error") or result.get("subtype") != "success":
        if any(sign in str(result.get("result")).lower() for sign in LIMIT_SIGNS):
            raise LimitReached(f"usage limit: {str(result.get('result'))[:200]}", cost)
        raise ModelError(f"CLI error: {result.get('subtype')}: {str(result.get('result'))[:300]}", cost)
    data = result.get("structured_output")
    if not isinstance(data, dict):
        raise ModelError("CLI result has no structured_output", cost)
    # bookkeeping fields must never fail a call that has already been paid for
    usage = result.get("modelUsage")
    usage = (
        {name: info for name, info in usage.items() if isinstance(info, dict)}
        if isinstance(usage, dict)
        else {}
    )
    # with a fallback the answer comes from whichever model wrote the most
    model = max(usage, key=lambda name: _int(usage[name].get("outputTokens")), default=call.model)
    tokens = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    return ModelReply(
        data=data,
        cost_usd=cost,
        model=model,
        input_tokens=sum(_int(tokens.get(key)) for key in INPUT_TOKEN_KEYS),
        output_tokens=_int(tokens.get("output_tokens")),
    )


def _result_line(stdout: str) -> dict[str, Any] | None:
    # stream-json prints one event per line; the answer and its cost are in the last "result" event
    for line in reversed(stdout.splitlines()):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def _media_type(path: Path) -> str:
    return IMAGE_TYPES[path.suffix.lower()]


def _check_image(path: Path) -> None:
    # checked before the call: a bad image must fail for free, not after the model has been paid
    if path.suffix.lower() not in IMAGE_TYPES:
        raise ValueError(f"image {path.name}: only png and jpeg are sent")
    if not path.is_file():
        raise ValueError(f"image {path} does not exist")
    if path.stat().st_size > MAX_IMAGE_BYTES:
        raise ValueError(f"image {path.name} is over {MAX_IMAGE_BYTES} bytes")


def _shape_problem(data: object, schema: dict[str, Any]) -> str | None:
    if not isinstance(data, dict):
        return "reply is not a JSON object"
    missing = set(schema.get("required", [])) - data.keys()
    return f"reply lacks required keys {sorted(missing)}" if missing else None


def _int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _cost(value: object, call: ModelCall) -> float:
    # an unreadable cost is unknown, and unknown counts as the whole cap
    try:
        cost = float(value or 0.0)
    except (TypeError, ValueError):
        return call.max_usd
    return cost if math.isfinite(cost) and cost >= 0 else call.max_usd


def resolve_binary() -> str:
    """Path of the pinned CLI in the npx cache; npx itself costs 0.8 s per start, the binary 0.01 s."""
    try:
        proc = subprocess.run(
            ["npx", "-y", "-p", CLI_PACKAGE, "-c", "command -v claude"],
            capture_output=True, text=True, timeout=300,
        )  # fmt: skip
    except (FileNotFoundError, subprocess.TimeoutExpired) as error:
        raise ModelError(f"cannot resolve {CLI_PACKAGE}: {error}") from None
    path = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    if proc.returncode != 0 or not path:
        raise ModelError(f"cannot resolve {CLI_PACKAGE}: {proc.stderr.strip()[:300]}")
    return path
