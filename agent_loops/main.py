import os
import subprocess
from dotenv import load_dotenv
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_openai import ChatOpenAI
from langchain_core.tools import tool

load_dotenv(override=True)
api_key = os.getenv("OPENAI_API_KEY")
base_url = os.getenv("OPENAI_BASE_URL")
model_id = os.getenv("MODEL_ID")

# Systems Prompts
SYSTEM = f"You are a coding agent at {os.getcwd()}. Use bash to solve tasks. Act, don't explain."


# ── Tool execution ────────────────────────────────────────
def _execute_bash(command: str) -> str:
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


@tool
def run_bash(command: str) -> str:
    """Run a shell command."""
    return _execute_bash(command)


# Model Definition
llm = ChatOpenAI(
    api_key=api_key,
    base_url=base_url,
    model=model_id # or OpenRouter model
)
llm_with_tools = llm.bind_tools([run_bash])


# ── The core pattern: loop until model stops calling tools ──
def agent_loop(messages: list):                                                                                     
      while True:                                                                                                     
          response = llm_with_tools.invoke(messages)
          messages.append(response)  # AIMessage                                                                      
                                                                                                                    
          if not response.tool_calls:          
              return
                                                                                                                      
          for tc in response.tool_calls:
              cmd = tc["args"]["command"]                                                                             
              print(f"\033[33m$ {cmd}\033[0m")                                                                      
              output = _execute_bash(cmd)                                                                                  
              print(output[:200])
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