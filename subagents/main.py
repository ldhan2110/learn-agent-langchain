import os, json, subprocess, ast
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool
from pydantic import BaseModel, Field
from typing import Literal

# Model & API Key definitions
load_dotenv(override=True)
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")
model_id = os.getenv("MODEL_ID")

WORKDIR = Path.cwd()

# Systems Prompts
SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Before starting any multi-step task, use run_todo_write to plan your steps. "
    "Update status as you go.\n\n"
    "DELEGATION RULE: When a task has 2+ independent parts, you MUST use run_task "
    "to spawn a subagent for each part instead of doing them sequentially yourself.\n"
    "Example: 'Read main.py and fix utils.py' → spawn run_task for each.\n"
    "Example: 'Find TODOs and summarize the project' → spawn run_task for each.\n"
    "Default to delegation. Only do work directly if it's a single, simple step."
)

# s06: subagent gets its own system prompt — no task, no recursion
SUB_SYSTEM = (
    f"You are a coding agent at {WORKDIR}. "
    "Complete the task you were given, then return a concise summary. "
    "Do not delegate further."
)

class TodoItem(BaseModel):
    content: str = Field(description="Task description")
    status: Literal["pending", "in_progress", "completed"] = Field(description="Task status")

# ── Tool execution ────────────────────────────────────────
def _normalize_todos(todos):
    if isinstance(todos, str):
        try:
            todos = json.loads(todos)
        except json.JSONDecodeError:
            try:
                todos = ast.literal_eval(todos)
            except (SyntaxError, ValueError):
                return None, "Error: todos must be a list or JSON array string"
    if not isinstance(todos, list):
        return None, "Error: todos must be a list"
    for i, t in enumerate(todos):
        if not isinstance(t, dict):
            return None, f"Error: todos[{i}] must be an object"
        if "content" not in t or "status" not in t:
            return None, f"Error: todos[{i}] missing 'content' or 'status'"
        if t["status"] not in ("pending", "in_progress", "completed"):
            return None, f"Error: todos[{i}] has invalid status '{t['status']}'"
    return todos, None

@tool
def run_todo_write(todos: list[TodoItem]) -> str:
    """Create and manage a task list for your current coding session."""
    global CURRENT_TODOS
    todos = [t.model_dump() if isinstance(t, TodoItem) else t for t in todos]
    todos, error = _normalize_todos(todos)
    if error:
        return error
    CURRENT_TODOS = todos
    lines = ["\n\033[33m## Current Tasks\033[0m"]
    for t in CURRENT_TODOS:
        icon = {"pending": " ", "in_progress": "\033[36m▸\033[0m", "completed": "\033[32m✓\033[0m"}[t["status"]]
        lines.append(f"  [{icon}] {t['content']}")
    print("\n".join(lines))
    return f"Updated {len(CURRENT_TODOS)} tasks"


@tool
def run_bash(command: str) -> str:
    """Run a shell command."""
    try:
        r = subprocess.run(command, shell=True, cwd=os.getcwd(),
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return f"Error: {e}"
    
def safe_path(p: str) -> Path:
    """Validate path stays within workspace."""
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

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
    
# ═══════════════════════════════════════════════════════════
#  s06: Subagent — fresh messages[], summary only
# ═══════════════════════════════════════════════════════════

# Subagent tools: no run_task (prevents recursion), no run_todo_write
SUB_TOOLS = [run_bash, run_read, run_write, run_edit, run_glob]

SUB_HANDLERS = {
    "run_bash": run_bash, "run_read": run_read, "run_write": run_write,
    "run_edit": run_edit, "run_glob": run_glob,
}

def spawn_subagent(description: str) -> str:
    """Spawn a subagent with fresh messages[], return summary only."""
    print(f"\n\033[35m[Subagent spawned]\033[0m")

    # Fresh LLM instance bound to sub-tools only
    sub_llm = ChatOpenAI(api_key=api_key, base_url=base_url, model=model_id)
    sub_llm_with_tools = sub_llm.bind_tools(SUB_TOOLS)

    # Fresh context — only the task description
    messages = [SystemMessage(content=SUB_SYSTEM), HumanMessage(content=description)]

    for _ in range(30):  # safety limit
        response = sub_llm_with_tools.invoke(messages)
        messages.append(response)

        if not response.tool_calls:
            break

        for tc in response.tool_calls:
            block = SimpleNamespace(name=tc["name"], input=tc["args"], id=tc["id"])

            blocked = trigger_hooks("PreToolUse", block)
            if blocked:
                messages.append(ToolMessage(content=str(blocked), tool_call_id=tc["id"]))
                continue

            handler = SUB_HANDLERS.get(tc["name"])
            output = handler.invoke(tc["args"]) if handler else f"Unknown: {tc['name']}"
            trigger_hooks("PostToolUse", block, output)
            print(f"  \033[90m[sub] {tc['name']}: {str(output)[:100]}\033[0m")
            messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))

    # Extract final text from last assistant message
    result = messages[-1].content if hasattr(messages[-1], "content") else ""
    if not result:
        for msg in reversed(messages):
            if hasattr(msg, "content") and not isinstance(msg, ToolMessage) and msg.content:
                result = msg.content
                break
        if not result:
            result = "Subagent stopped after 30 turns without final answer."

    print(f"\033[35m[Subagent done]\033[0m")
    return result

@tool
def run_task(description: str) -> str:
    """Launch a subagent to handle a complex subtask. Returns only the final conclusion."""
    return spawn_subagent(description)

TOOL_HANDLERS = {
    "run_bash": run_bash,
    "run_read": run_read,
    "run_write": run_write,
    "run_edit": run_edit,
    "run_glob": run_glob,
    "run_todo_write": run_todo_write,
    "run_task": run_task,
}

# ── Hooks Gate ────────────────────────────────────────
HOOKS = {"UserPromptSubmit": [], "PreToolUse": [], "PostToolUse": [], "Stop": []}

def register_hook(event: str, callback):
    HOOKS[event].append(callback)

def trigger_hooks(event: str, *args):
    for callback in HOOKS[event]:
        result = callback(*args)
        if result is not None:  # teaching shortcut: block this tool call
            return result
    return None


# ── Permission ────────────────────────────────────────
# Gate 1: Hard deny list — always forbidden
DENY_LIST = ["rm -rf /", "sudo", "shutdown", "reboot", "mkfs", "dd if="]
DESTRUCTIVE = ["rm ", "> /etc/", "chmod 777"]

def permission_hook(block):
    """PreToolUse: s03 check_permission() logic moved here."""
    if block.name == "bash":
        for pattern in DENY_LIST:
            if pattern in block.input.get("command", ""):
                print(f"\n\033[31m⛔ Blocked: '{pattern}'\033[0m")
                return "Permission denied by deny list"
        for kw in DESTRUCTIVE:
            if kw in block.input.get("command", ""):
                print(f"\n\033[33m⚠  Potentially destructive command\033[0m")
                print(f"   Tool: {block.name}({block.input})")
                choice = input("   Allow? [y/N] ").strip().lower()
                if choice not in ("y", "yes"):
                    return "Permission denied by user"
    if block.name in ("write_file", "edit_file"):
        path = block.input.get("path", "")
        if not (WORKDIR / path).resolve().is_relative_to(WORKDIR):
            print(f"\n\033[33m⚠  Writing outside workspace\033[0m")
            print(f"   Tool: {block.name}({block.input})")
            choice = input("   Allow? [y/N] ").strip().lower()
            if choice not in ("y", "yes"):
                return "Permission denied by user"
    return None

def log_hook(block):
    """PreToolUse: log every tool call."""
    args_preview = str(list(block.input.values())[:2])[:60]
    print(f"\033[90m[HOOK] {block.name}({args_preview})\033[0m")
    return None

def large_output_hook(block, output):
    """PostToolUse: warn on large output."""
    if len(str(output)) > 100000:
        print(f"\033[33m[HOOK] ⚠ Large output from {block.name}: {len(str(output))} chars\033[0m")
    return None

# UserPromptSubmit hook: log user input before it reaches the LLM
def context_inject_hook(query: str):
    print(f"\033[90m[HOOK] UserPromptSubmit: working in {WORKDIR}\033[0m")
    return None

# Stop hook: print summary when loop is about to exit
def summary_hook(messages: list):
    tool_count = sum(1 for m in messages if isinstance(m, ToolMessage))
    print(f"\033[90m[HOOK] Stop: session used {tool_count} tool calls\033[0m")
    return None

register_hook("UserPromptSubmit", context_inject_hook)
register_hook("PreToolUse", permission_hook)
register_hook("PreToolUse", log_hook)
register_hook("PostToolUse", large_output_hook)
register_hook("Stop", summary_hook)


# ── Todo layer - Harness Layer ────────────────────────────────────────
CURRENT_TODOS: list[dict] = []

# Model Definition
llm = ChatOpenAI(
    api_key=api_key,
    base_url=base_url,
    model=model_id # or OpenRouter model
)
llm_with_tools = llm.bind_tools([run_bash, run_edit, run_glob, run_read, run_write, run_todo_write, run_task])

rounds_since_todo = 0

# ── The core pattern: loop until model stops calling tools ──
def agent_loop(messages: list):  
      global rounds_since_todo                                                                                   
      while True:           
          if rounds_since_todo >= 3 and messages:
            messages.append({"role": "user",
                             "content": "<reminder>Update your todos.</reminder>"})
            rounds_since_todo = 0

          response = llm_with_tools.invoke(messages)
          messages.append(response)  # AIMessage                                                                      
                                                                                                                    
          if not response.tool_calls:
              force = trigger_hooks("Stop", messages)
              if force:
                  messages.append(HumanMessage(content=force))
                  continue
              return

          for tc in response.tool_calls:
              block = SimpleNamespace(name=tc["name"], input=tc["args"], id=tc["id"])

              # PreToolUse hook (equivalent to Anthropic SDK's block-level gate)
              blocked = trigger_hooks("PreToolUse", block)
              if blocked:
                  messages.append(ToolMessage(content=str(blocked), tool_call_id=tc["id"]))
                  continue

              handler = TOOL_HANDLERS.get(tc["name"])
              output = handler.invoke(tc["args"]) if handler else f"Unknown: {tc['name']}"
              print(str(output)[:200])

              # PostToolUse hook
              trigger_hooks("PostToolUse", block, output)

              messages.append(ToolMessage(content=str(output), tool_call_id=tc["id"]))                   

# Execution
if __name__ == "__main__":                   
      print("s06: Subagent — spawn sub-agents with fresh context (LangChain)")                                                                                  
      history = [SystemMessage(content=SYSTEM)]                                                                       
      while True:                                                                                                     
          try:                                                                                                        
              query = input("\033[36magent >> \033[0m")                                                             
          except (EOFError, KeyboardInterrupt):                                                                       
              break
          if query.strip().lower() in ("q", "exit", ""):                                                              
              break          
          trigger_hooks("UserPromptSubmit", query)                                                                                   
          history.append(HumanMessage(content=query))
          agent_loop(history)                                                                                         
          # Print final text response                                                                               
          last = history[-1]                                                                                          
          if hasattr(last, "content") and last.content:                                                             
              print(last.content)                                                                                     
          print()            