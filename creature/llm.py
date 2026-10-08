"""Model calls for the spine: isolated, schema-shaped, budget-capped and logged.

Every call runs the pinned Claude Code CLI with its own system prompt, no tools, no MCP servers and no
user settings, from an empty directory, so the model sees nothing but what the spine sends.

Caps: before each call the run's remaining budget must cover a reserve (the cost of a large call at
fallback-model prices); otherwise the call is refused and the model is never reached. The CLI checks
--max-budget-usd only after a turn, so it is a second net: a call over its cap still costs, then
returns no result. A run can therefore overshoot its budget by at most one call.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Any, Protocol

from creature.ledger import Ledger

CLI_PACKAGE = "@anthropic-ai/claude-code@2.1.294"
MODEL = "claude-haiku-5-5"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"
RESERVE_USD = 0.03  # ~4k output tokens on the fallback model, plus input
SECRET_ENV = ("CREATURE_SECRETS", "APIFY_TOKEN", "ELEVENLABS_API_KEY")
INPUT_TOKEN_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")


@dataclass(frozen=True)
class ModelCall:
    step: str  # spine step that asks: "planner", "examiner", "forge", "perceive", "skill:<name>"
    model: str
    system: str
    prompt: str
    schema: dict[str, Any]
    max_usd: float


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


class Model:
    """The only way the spine reaches a model. Enforces the run budget and logs every call."""

    def __init__(
        self,
        transport: Transport,
        ledger: Ledger,
        *,
        budget_usd: float,
        reserve_usd: float = RESERVE_USD,
        model: str = MODEL,
    ) -> None:
        self.transport = transport
        self.ledger = ledger
        self.budget_usd = budget_usd
        self.reserve_usd = reserve_usd
        self.model = model

    @property
    def remaining_usd(self) -> float:
        return self.budget_usd - self.ledger.spent_usd

    def ask(
        self, step: str, system: str, prompt: str, schema: dict[str, Any], *, cap_usd: float
    ) -> dict[str, Any]:
        """Structured answer for one step, or ModelError. Never spends past the caps it was given."""
        remaining = self.remaining_usd
        if remaining < self.reserve_usd:
            self.ledger.record("model_refused", step=step, reason="budget", remaining_usd=round(remaining, 6))
            raise BudgetRefused(f"{step}: ${remaining:.4f} left, a call needs a ${self.reserve_usd} reserve")
        call = ModelCall(step, self.model, system, prompt, schema, max_usd=round(min(cap_usd, remaining), 6))
        try:
            reply = self.transport.complete(call)
        except ModelError as error:
            self.ledger.record(
                "model_call", step=step, model=call.model, ok=False, error=str(error)[:500],
                max_usd=call.max_usd, cost_usd=error.cost_usd,
            )  # fmt: skip
            raise
        ok = isinstance(reply.data, dict)
        self.ledger.record(
            "model_call", step=step, model=reply.model, ok=ok, max_usd=call.max_usd, cost_usd=reply.cost_usd,
            input_tokens=reply.input_tokens, output_tokens=reply.output_tokens,
        )  # fmt: skip
        if not ok:
            raise ModelError(f"{step}: reply is not a JSON object")
        return reply.data


class ClaudeCLI:
    """Transport over the pinned Claude Code CLI (`claude -p`)."""

    def __init__(self, binary: str | None = None, *, timeout_s: float = 180.0) -> None:
        self.binary = binary or os.environ.get("CREATURE_CLAUDE") or resolve_binary()
        self.timeout_s = timeout_s

    def command(self, call: ModelCall) -> list[str]:
        # the prompt goes over stdin: a prompt starting with "--" must never be read as a flag
        return [
            self.binary, "-p",
            "--model", call.model, "--fallback-model", FALLBACK_MODEL,
            "--system-prompt", call.system,
            "--tools", "", "--strict-mcp-config", "--setting-sources", "",
            "--output-format", "json", "--json-schema", json.dumps(call.schema),
            "--max-budget-usd", f"{call.max_usd:.6f}",
        ]  # fmt: skip

    def complete(self, call: ModelCall) -> ModelReply:
        env = {key: value for key, value in os.environ.items() if key not in SECRET_ENV}
        with tempfile.TemporaryDirectory(prefix="creature-model-") as empty:
            try:
                proc = subprocess.run(
                    self.command(call), input=call.prompt, capture_output=True, text=True,
                    cwd=empty, env=env, timeout=self.timeout_s,
                )  # fmt: skip
            except subprocess.TimeoutExpired:
                # the cost of a killed call is unknown: count the whole cap
                raise ModelError(f"timeout after {self.timeout_s}s", cost_usd=call.max_usd) from None
        return parse(proc.stdout, call, stderr=proc.stderr)


def parse(stdout: str, call: ModelCall, *, stderr: str = "") -> ModelReply:
    """ModelReply from the CLI's JSON result, or ModelError carrying whatever the call cost."""
    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        raise ModelError(f"CLI gave no JSON: {(stdout or stderr).strip()[:300]}") from None
    if not isinstance(result, dict):
        raise ModelError("CLI result is not an object")
    cost = float(result.get("total_cost_usd") or 0.0)
    if result.get("is_error") or result.get("subtype") != "success":
        raise ModelError(f"CLI error: {result.get('subtype')}: {str(result.get('result'))[:300]}", cost)
    data = result.get("structured_output")
    if not isinstance(data, dict):
        raise ModelError("CLI result has no structured_output", cost)
    usage = result.get("modelUsage") or {}
    # with a fallback the answer comes from whichever model wrote the most
    model = max(usage, key=lambda name: usage[name].get("outputTokens", 0), default=call.model)
    tokens = result.get("usage") or {}
    return ModelReply(
        data=data,
        cost_usd=cost,
        model=model,
        input_tokens=sum(int(tokens.get(key, 0)) for key in INPUT_TOKEN_KEYS),
        output_tokens=int(tokens.get("output_tokens", 0)),
    )


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
