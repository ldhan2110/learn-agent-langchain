import os
import subprocess
from pathlib import Path
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
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
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
              return
                                                                                                                      
          for tc in response.tool_calls:
              name = tc["name"]
              args = tc["args"]
              print(f"\033[33m> {name}({args})\033[0m")
              handler = TOOL_HANDLERS.get(name)
              output = handler.invoke(args) if handler else f"Unknown tool: {name}"
              print(str(output)[:200])
              messages.append(ToolMessage(content=output, tool_call_id=tc["id"]))                   

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
          history.append(HumanMessage(content=query))
          agent_loop(history)                                                                                         
          # Print final text response                                                                               
          last = history[-1]                                                                                          
          if hasattr(last, "content") and last.content:                                                             
              print(last.content)                                                                                     
          print()            