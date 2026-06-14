"""
Production-grade permission system for a LangChain coding agent.

Upgrades over main.py:
  - shlex token parsing instead of substring matching
  - Tiered decisions (ALLOW / PROMPT / DENY)
  - Structured audit logging to audit.log
  - External YAML config (permissions.yaml)
  - Per-tool rate limiting
  - Shell injection pattern detection
"""

import os
import json
import shlex
import subprocess
import logging
import time
import yaml
from enum import Enum
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone

from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

# ── Config ────────────────────────────────────────────────
load_dotenv(override=True)
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")
model_id = os.getenv("MODEL_ID")

WORKDIR = Path.cwd()
SYSTEM = f"You are a coding agent at {WORKDIR}. Use bash to solve tasks. Act, don't explain."


# ── Audit Logger ──────────────────────────────────────────
# Writes structured JSON lines to audit.log — every decision recorded.
# Why structured? grep/jq can filter by tool, decision, time range.
audit_log = logging.getLogger("permissions.audit")
audit_log.setLevel(logging.INFO)
_handler = logging.FileHandler(WORKDIR / "audit.log")
_handler.setFormatter(logging.Formatter("%(message)s"))  # raw JSON lines
audit_log.addHandler(_handler)


def log_decision(tool_name: str, args: dict, decision: str, reason: str):
    """Record every permission decision — allows post-incident investigation."""
    audit_log.info(json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name,
        "args": args,
        "decision": decision,
        "reason": reason,
    }))


# ── Decision Tiers ────────────────────────────────────────
# Why enum instead of string? Typos become errors at import time, not runtime.
class Decision(Enum):
    ALLOW = "allow"        # pass through, no prompt
    PROMPT = "prompt"      # ask user y/N
    DENY = "deny"          # hard block, tell model why


# ── Load External Config ──────────────────────────────────
# Why external? Change rules without touching code. Different envs get different rules.
def load_permissions(path: str = "permissions.yaml") -> dict:
    config_path = WORKDIR / path
    if not config_path.exists():
        print(f"\033[33m⚠ No {path} found, using defaults\033[0m")
        return {
            "deny_always": [
                "rm -rf /", "mkfs", "dd if=/dev/", "shutdown", "reboot",
                "> /dev/sda", "--no-preserve-root"
            ],
            "dangerous_binaries": ["su", "sudo", "pkill", "killall"],
            "dangerous_flags": {"rm": ["-rf", "-fr", "--no-preserve-root"]},
            "shell_injection_patterns": ["| sh", "| bash", "eval ", "$(", "`"],
            "require_approval_patterns": ["rm ", "> /etc/", "chmod 777", "curl ", "wget "],
            "auto_allow_tools": ["run_read", "run_glob"],
            "rate_limits": {"run_bash": {"max_calls": 30, "window_seconds": 60}},
        }
    with open(config_path) as f:
        return yaml.safe_load(f)


PERMISSIONS = load_permissions()


# ── Tool Definitions ──────────────────────────────────────
def safe_path(p: str) -> Path:
    """Validate path stays within workspace."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


@tool
def run_bash(command: str) -> str:
    """Run a shell command."""
    try:
        r = subprocess.run(command, shell=True, cwd=str(WORKDIR),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


@tool
def run_read(path: str, limit: int | None = None) -> str:
    """Read file contents."""
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


@tool
def run_write(path: str, content: str) -> str:
    """Write content to a file."""
    try:
        file_path = safe_path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return f"Error: {e}"


@tool
def run_edit(path: str, old_text: str, new_text: str) -> str:
    """Replace exact text in a file once."""
    try:
        file_path = safe_path(path)
        text = file_path.read_text()
        if old_text not in text:
            return f"Error: text not found in {path}"
        file_path.write_text(text.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


@tool
def run_glob(pattern: str) -> str:
    """Find files matching a glob pattern."""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


TOOL_HANDLERS = {
    "run_bash": run_bash,
    "run_read": run_read,
    "run_write": run_write,
    "run_edit": run_edit,
    "run_glob": run_glob,
}


# ── Gate 1: Token-Aware Command Parser ────────────────────
# Why shlex instead of substring? "pseudocode" won't trigger "sudo".
# Parses command into tokens, checks binary name and flags separately.
def check_command_tokens(command: str) -> tuple[Decision, str]:
    # First: literal deny strings (catches even inside pipes/chains)
    for pattern in PERMISSIONS.get("deny_always", []):
        if pattern in command:
            return Decision.DENY, f"Blocked: contains '{pattern}'"

    # Shell injection patterns — model might try to sneak past guards
    for pattern in PERMISSIONS.get("shell_injection_patterns", []):
        if pattern in command:
            return Decision.PROMPT, f"Shell injection pattern: '{pattern}'"

    # Token-level analysis
    try:
        tokens = shlex.split(command)
    except ValueError:
        # Unbalanced quotes, unclosed strings — suspicious
        return Decision.PROMPT, "Unparseable command (unbalanced quotes)"

    if not tokens:
        return Decision.ALLOW, ""

    binary = Path(tokens[0]).name  # "/usr/bin/rm" → "rm"

    # Check dangerous binaries
    dangerous_bins = PERMISSIONS.get("dangerous_binaries", [])
    if binary in dangerous_bins:
        return Decision.DENY, f"Blocked: '{binary}' is a dangerous binary"

    # Check dangerous flags for specific binaries
    dangerous_flags = PERMISSIONS.get("dangerous_flags", {})
    if binary in dangerous_flags:
        flags = {t for t in tokens[1:] if t.startswith("-")}
        matched = flags & set(dangerous_flags[binary])
        if matched:
            return Decision.DENY, f"Blocked: '{binary}' with flags {matched}"

    # Soft checks — require approval but don't hard-block
    for pattern in PERMISSIONS.get("require_approval_patterns", []):
        if pattern in command:
            return Decision.PROMPT, f"Needs approval: contains '{pattern}'"

    return Decision.ALLOW, ""


# ── Gate 2: Path Escape Check ─────────────────────────────
def check_path_escape(tool_name: str, args: dict) -> tuple[Decision, str]:
    if tool_name not in ("run_write", "run_edit"):
        return Decision.ALLOW, ""
    path_arg = args.get("path", "")
    try:
        resolved = (WORKDIR / path_arg).resolve()
        if not resolved.is_relative_to(WORKDIR):
            return Decision.DENY, f"Path escapes workspace: {path_arg}"
    except (ValueError, OSError):
        return Decision.DENY, f"Invalid path: {path_arg}"
    return Decision.ALLOW, ""


# ── Gate 3: Rate Limiter ──────────────────────────────────
# Why? Model might spam tool calls to brute-force past blocks or
# exhaust system resources. Per-tool sliding window.
_call_timestamps: dict[str, list[float]] = defaultdict(list)


def check_rate_limit(tool_name: str) -> tuple[Decision, str]:
    limits = PERMISSIONS.get("rate_limits", {})
    if tool_name not in limits:
        return Decision.ALLOW, ""

    max_calls = limits[tool_name]["max_calls"]
    window = limits[tool_name]["window_seconds"]
    now = time.time()

    # Sliding window: drop old timestamps, count recent ones
    _call_timestamps[tool_name] = [
        t for t in _call_timestamps[tool_name] if now - t < window
    ]
    if len(_call_timestamps[tool_name]) >= max_calls:
        return Decision.DENY, f"Rate limit: {tool_name} called {max_calls}x in {window}s"

    _call_timestamps[tool_name].append(now)
    return Decision.ALLOW, ""


# ── Gate 4: User Approval ─────────────────────────────────
def ask_user(tool_name: str, args: dict, reason: str) -> Decision:
    print(f"\n\033[33m⚠  {reason}\033[0m")
    print(f"   Tool: {tool_name}")
    # Show relevant arg, not the whole dict (cleaner UX)
    if tool_name == "run_bash":
        print(f"   Command: {args.get('command', '')}")
    else:
        print(f"   Args: {args}")
    choice = input("   Allow? [y/N] ").strip().lower()
    return Decision.ALLOW if choice in ("y", "yes") else Decision.DENY


# ── Permission Pipeline ──────────────────────────────────
# All gates chained. Order matters:
#   rate limit → command parse → path escape → user prompt
#
# Why this order?
#   - Rate limit first: cheap check, stops spam before any parsing
#   - Command parse: catches dangerous commands before they hit user prompt
#   - Path escape: catches file-tool specific risks
#   - User prompt last: only for ambiguous cases that survived all checks

def check_permission(tool_call: dict) -> bool:
    name = tool_call["name"]
    args = tool_call["args"]

    # Auto-allow safe tools (read, glob) — skip all gates
    if name in PERMISSIONS.get("auto_allow_tools", []):
        log_decision(name, args, "allow", "auto-allow tool")
        return True

    # Gate 1: Rate limit
    decision, reason = check_rate_limit(name)
    if decision == Decision.DENY:
        print(f"\n\033[31m⛔ {reason}\033[0m")
        log_decision(name, args, "deny", reason)
        return False

    # Gate 2: Command token analysis (bash only)
    if name == "run_bash":
        decision, reason = check_command_tokens(args.get("command", ""))
        if decision == Decision.DENY:
            print(f"\n\033[31m⛔ {reason}\033[0m")
            log_decision(name, args, "deny", reason)
            return False
        if decision == Decision.PROMPT:
            final = ask_user(name, args, reason)
            log_decision(name, args, final.value, reason)
            return final == Decision.ALLOW

    # Gate 3: Path escape (write/edit only)
    decision, reason = check_path_escape(name, args)
    if decision == Decision.DENY:
        print(f"\n\033[31m⛔ {reason}\033[0m")
        log_decision(name, args, "deny", reason)
        return False

    log_decision(name, args, "allow", "passed all gates")
    return True


# ── Model ─────────────────────────────────────────────────
llm = ChatOpenAI(api_key=api_key, base_url=base_url, model=model_id)
llm_with_tools = llm.bind_tools([run_bash, run_edit, run_glob, run_read, run_write])


# ── Agent Loop ────────────────────────────────────────────
def agent_loop(messages: list):
    while True:
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            return

        for tc in response.tool_calls:
            name = tc["name"]
            args = tc["args"]
            print(f"\033[33m> {name}({args})\033[0m")

            if not check_permission(tc):
                messages.append(ToolMessage(
                    content="Permission denied.",
                    tool_call_id=tc["id"],
                ))
                continue

            handler = TOOL_HANDLERS.get(name)
            output = handler.invoke(args) if handler else f"Unknown tool: {name}"
            print(str(output)[:200])
            messages.append(ToolMessage(content=output, tool_call_id=tc["id"]))


# ── Main ──────────────────────────────────────────────────
if __name__ == "__main__":
    print("Agent Loop — Production Permissions (LangChain)")
    history = [SystemMessage(content=SYSTEM)]
    while True:
        try:
            query = input("\033[36magent >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append(HumanMessage(content=query))
        agent_loop(history)
        last = history[-1]
        if hasattr(last, "content") and last.content:
            print(last.content)
        print()
