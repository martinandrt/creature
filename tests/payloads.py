"""Allow/deny fixtures for the static gate (gate.py, F5), as data.

The gate decides, before any run, whether a generated code skill is even allowed into the
sandbox. Its rules (PLAN §5/§6): only the allowlisted imports, no banned builtins, and no
"__" token anywhere (identifier, attribute or string constant). These are ordinary allowlist
and denylist cases, not exploits; runtime containment is the sandbox's job and is tested there.

Allowed imports for code skills:
    re json math datetime collections itertools string unicodedata statistics
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GatePayload:
    id: str
    code: str
    verdict: str  # "allow" or "reject"
    note: str


# Rejected: an import outside the allowlist, a banned builtin, or any "__" token.
_REJECT = [
    GatePayload("import_os", "import os\ndef run(i): return i['x']", "reject", "import outside allowlist"),
    GatePayload("import_socket", "import socket\ndef run(i): return i['x']", "reject", "network import"),
    GatePayload(
        "import_subprocess", "import subprocess\ndef run(i): return i['x']", "reject", "process import"
    ),
    GatePayload("import_builtins", "import builtins\ndef run(i): return i['x']", "reject", "builtins import"),
    GatePayload(
        "from_import_os",
        "from os import getcwd\ndef run(i): return getcwd()",
        "reject",
        "from-import outside allowlist",
    ),
    GatePayload("call_eval", 'def run(i): return eval("1+1")', "reject", "eval banned"),
    GatePayload("call_exec", 'def run(i): exec("x=1"); return 1', "reject", "exec banned"),
    GatePayload("call_compile", 'def run(i): return compile("1", "<s>", "eval")', "reject", "compile banned"),
    GatePayload("call_open", "def run(i): return open(i['path']).read()", "reject", "open banned"),
    GatePayload(
        "call_import_builtin", 'def run(i): return __import__("math")', "reject", "__import__ banned"
    ),
    GatePayload("call_getattr", "def run(i): return getattr(i, 'x')", "reject", "getattr banned"),
    GatePayload("call_vars", "def run(i): return vars(i)", "reject", "vars banned"),
    GatePayload("call_globals", "def run(i): return globals()", "reject", "globals banned"),
    GatePayload("call_input", "def run(i): return input()", "reject", "input banned"),
    GatePayload("call_breakpoint", "def run(i): breakpoint(); return 1", "reject", "breakpoint banned"),
    GatePayload(
        "dunder_name_guard",
        'def run(i): return 1\nif __name__ == "__main__": run({})',
        "reject",
        "__ token in a name",
    ),
    GatePayload(
        "dunder_in_string", 'def run(i): return "__secret__"', "reject", "__ token in a string constant"
    ),
    GatePayload("no_run", "x = 1", "reject", "no run(input) entry point"),
]

# Allowed: clean skills that use only the allowlist. A gate that rejects everything is useless.
_ALLOW = [
    GatePayload("plain_add", "def run(i): return i['a'] + i['b']", "allow", "pure arithmetic"),
    GatePayload(
        "regex", 'import re\ndef run(i): return re.findall(r"\\d+", i["text"])', "allow", "allowed import re"
    ),
    GatePayload("math", "import math\ndef run(i): return math.sqrt(i['n'])", "allow", "allowed import math"),
    GatePayload(
        "counter",
        "from collections import Counter\ndef run(i): return dict(Counter(i['xs']))",
        "allow",
        "allowed from-import",
    ),
    GatePayload(
        "string_import",
        "import string\ndef run(i): return string.ascii_lowercase",
        "allow",
        "allowed import string",
    ),
    GatePayload(
        "single_underscore",
        "def run(i):\n    _tmp = i['x'] * 2\n    return _tmp",
        "allow",
        "single underscore is fine",
    ),
]

GATE_PAYLOADS: list[GatePayload] = [*_REJECT, *_ALLOW]
