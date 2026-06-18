"""
Production-grade agent loop with concurrent tool execution.
Mirrors Claude Code's behavior: parallel reads, serial writes.
"""

import os
import subprocess
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

load_dotenv(override=True)
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")
model_id = os.getenv("MODEL_ID")

WORKDIR = Path.cwd()
MAX_TOOL_OUTPUT = 50000  # fed back to LLM
MAX_PRINT_OUTPUT = 200   # console display

SYSTEM = (
    f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain. "
    "When multiple independent tool calls are needed, call them all in a single response "
    "to maximize efficiency. For example, reading 3 files = 3 tool calls in one response."
)


# ── Concurrency classification ─────────────────────────────
# CC pattern: not just "read vs write" — judges by tool + input

READ_ONLY_TOOLS = {"run_read", "run_glob"}

def is_concurrency_safe(name: str, args: dict) -> bool:
    """Determine if tool call can run concurrently.

    Read-only tools: always safe.
    Bash: safe if command looks read-only.
    Write tools: safe only if targeting different files (handled at batch level).
    """
    if name in READ_ONLY_TOOLS:
        return True
    if name == "run_bash":
        cmd = args.get("command", "")
        read_prefixes = ("ls", "cat", "head", "tail", "grep", "find", "wc",
                         "echo", "pwd", "which", "env", "printenv", "date",
                         "git status", "git log", "git diff", "git show")
        return any(cmd.strip().startswith(p) for p in read_prefixes)
    return False


# ── Tool implementations ──────────────────────────────────

def safe_path(p: str) -> Path:
    """Validate path stays within workspace."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


@tool
def run_bash(command: str) -> str:
    """Run a shell command."""
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:MAX_TOOL_OUTPUT] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"


@tool
def run_read(path: str, limit: int = 2000, offset: int = 0) -> str:
    """Read file contents with pagination. Default first 2000 lines."""
    try:
        lines = safe_path(path).read_text().splitlines()
        total = len(lines)
        lines = lines[offset:offset + limit]
        header = f"[Lines {offset + 1}-{offset + len(lines)} of {total}]"
        if offset + limit < total:
            header += f" (use offset={offset + limit} to read more)"
        return header + "\n" + "\n".join(lines)
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
def run_glob(pattern: str, head_limit: int = 250) -> str:
    """Find files matching a glob pattern. Returns up to head_limit paths."""
    import glob as g
    try:
        results = []
        for match in g.glob(pattern, root_dir=WORKDIR, recursive=True):
            if (WORKDIR / match).resolve().is_relative_to(WORKDIR):
                results.append(match)
                if len(results) >= head_limit:
                    results.append(f"... (truncated at {head_limit})")
                    break
        return "\n".join(results) if results else "(no matches)"
    except Exception as e:
        return f"Error: {e}"


TOOLS = [run_bash, run_read, run_write, run_edit, run_glob]

TOOL_HANDLERS = {t.name: t for t in TOOLS}


# ── Concurrent tool execution ─────────────────────────────

def execute_one(tc: dict) -> ToolMessage:
    """Execute single tool call, return ToolMessage. Never raises."""
    name = tc["name"]
    args = tc["args"]
    handler = TOOL_HANDLERS.get(name)
    try:
        output = handler.invoke(args) if handler else f"Unknown tool: {name}"
    except Exception as e:
        output = f"Error: {e}"
    # Truncate before feeding back to LLM
    output = str(output)[:MAX_TOOL_OUTPUT]
    return ToolMessage(content=output, tool_call_id=tc["id"])


async def execute_one_async(tc: dict) -> ToolMessage:
    """Run tool in thread pool for true concurrency."""
    return await asyncio.get_event_loop().run_in_executor(None, execute_one, tc)


async def execute_tool_calls(tool_calls: list[dict]) -> list[ToolMessage]:
    """Execute tool calls with CC-style concurrency.

    Strategy:
    1. Partition into concurrent-safe vs serial calls
    2. Run all concurrent-safe calls in parallel
    3. Run serial calls one-by-one after
    """
    concurrent = []
    serial = []

    for tc in tool_calls:
        if is_concurrency_safe(tc["name"], tc["args"]):
            concurrent.append(tc)
        else:
            serial.append(tc)

    results = []

    # Phase 1: parallel reads
    if concurrent:
        tasks = [execute_one_async(tc) for tc in concurrent]
        parallel_results = await asyncio.gather(*tasks)
        for tc, result in zip(concurrent, parallel_results):
            print(f"\033[32m⟂ {tc['name']}({_brief(tc['args'])})\033[0m")  # green = concurrent
            print(str(result.content)[:MAX_PRINT_OUTPUT])
        results.extend(parallel_results)

    # Phase 2: serial writes
    for tc in serial:
        print(f"\033[33m→ {tc['name']}({_brief(tc['args'])})\033[0m")  # yellow = serial
        result = execute_one(tc)
        print(str(result.content)[:MAX_PRINT_OUTPUT])
        results.append(result)

    return results


def _brief(args: dict) -> str:
    """Short repr of args for console."""
    parts = []
    for k, v in args.items():
        s = str(v)
        parts.append(f"{k}={s[:40]}{'…' if len(s) > 40 else ''}")
    return ", ".join(parts)


# ── Model setup ───────────────────────────────────────────

llm = ChatOpenAI(api_key=api_key, base_url=base_url, model=model_id)
llm_with_tools = llm.bind_tools(TOOLS)


# ── Agent loop with concurrent execution ──────────────────

async def agent_loop(messages: list):
    while True:
        response = llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            return

        results = await execute_tool_calls(response.tool_calls)
        messages.extend(results)


# ── Entry point ───────────────────────────────────────────

async def main():
    print("Agent Loop (LangChain + Concurrent Tools)")
    history = [SystemMessage(content=SYSTEM)]
    while True:
        try:
            query = input("\033[36magent >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append(HumanMessage(content=query))
        await agent_loop(history)
        last = history[-1]
        if hasattr(last, "content") and last.content:
            print(last.content)
        print()


if __name__ == "__main__":
    asyncio.run(main())
