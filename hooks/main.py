import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

# Model & API Key definitions
load_dotenv(override=True)
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")
model_id = os.getenv("MODEL_ID")

WORKDIR = Path.cwd()

# Systems Prompts
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."


# ── Tool execution ────────────────────────────────────────
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
    
TOOL_HANDLERS = {
    "run_bash": run_bash, 
    "run_read": run_read, 
    "run_write": run_write,
    "run_edit": run_edit, 
    "run_glob": run_glob,
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


# ── Permission Hooks ────────────────────────────────────────
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


# Model Definition
llm = ChatOpenAI(
    api_key=api_key,
    base_url=base_url,
    model=model_id # or OpenRouter model
)
llm_with_tools = llm.bind_tools([run_bash, run_edit, run_glob, run_read, run_write])


# ── The core pattern: loop until model stops calling tools ──
def agent_loop(messages: list):                                                                                     
      while True:                                                                                                     
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
      print("Agent Loop (LangChain)")                                                                                  
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