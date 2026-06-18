# Subagents for AI Agents — Learning Notes

## What is a subagent?

Think of it like a manager hiring a temp worker.

Your **main agent** is doing a big job. It hits a sub-problem that needs focused work.
Instead of doing everything itself (and filling up its memory with noise),
it **spawns a new agent** with:

- A fresh brain (new LLM instance)
- A fresh notepad (empty message history)
- Only the sub-problem as instructions
- A limited set of tools (no ability to spawn more agents)

The subagent works, finishes, and sends back **only the final answer**.
The main agent never sees the subagent's intermediate steps — just the summary.

```
Main Agent                          Subagent
─────────                          ──────────
"I need to refactor foo.py"
    │
    ├─ run_task("refactor foo.py")
    │        │
    │        ├─ [fresh messages[]]
    │        ├─ run_read("foo.py")
    │        ├─ run_edit(...)        ← main agent never sees this
    │        ├─ run_read("foo.py")
    │        │
    │        └─ returns: "Refactored foo.py: extracted 3 functions"
    │
    ├─ continues with summary only
```

---

## Why not just let the main agent do everything?

| Problem | What happens | Subagent fix |
|---------|-------------|-------------|
| **Context pollution** | 50 tool calls for a subtask fill the main agent's memory with noise. Later reasoning degrades. | Subagent eats the noise. Main agent only sees the 1-line summary. |
| **Focus drift** | Agent forgets the big picture while deep in a subtask | Subagent handles the subtask. Main agent stays at the planning level. |
| **Token cost** | Every message stays in history forever. 50 tool calls = 50 messages in every future API call. | Subagent's 50 messages are discarded after it returns. Main agent adds only 1 message (the summary). |
| **Safety** | Main agent has `run_task` — could spawn agents forever (recursion bomb) | Subagent has NO `run_task` tool. Can't recurse. |

**Mental model:** Subagent = function call with its own stack frame. When it returns, the stack frame is gone. Only the return value survives.

---

## The code, piece by piece

### 1. Two system prompts — parent vs child

```python
SYSTEM = "You are a coding agent... use run_task for complex sub-problems."
SUB_SYSTEM = "Complete the task... Do not delegate further."
```

**Why two?** The main agent needs to know it *can* delegate. The subagent needs to know it *cannot*. Same LLM, different instructions.

### 2. Two tool sets — the recursion firewall

```python
# Main agent: 7 tools (including run_task)
llm.bind_tools([run_bash, run_edit, run_glob, run_read, run_write, run_todo_write, run_task])

# Subagent: 5 tools (NO run_task, NO run_todo_write)
SUB_TOOLS = [run_bash, run_read, run_write, run_edit, run_glob]
```

**Why remove `run_task`?** If the subagent could spawn its own subagents, you get infinite recursion. Agent spawns agent spawns agent → infinite loop → infinite API bills.

**Why remove `run_todo_write`?** Subagent is a temp worker. It doesn't manage the project plan. Only the main agent tracks tasks.

### 3. Fresh context — the whole point

```python
def spawn_subagent(description: str) -> str:
    # New LLM instance
    sub_llm = ChatOpenAI(api_key=api_key, base_url=base_url, model=model_id)

    # Fresh messages — ONLY the task
    messages = [SystemMessage(content=SUB_SYSTEM), HumanMessage(content=description)]
```

**This is the key insight.** The subagent starts with zero history. No baggage from the main conversation. Just: "here's your job, do it."

### 4. The subagent loop — same pattern, bounded

```python
for _ in range(30):  # safety limit
    response = sub_llm_with_tools.invoke(messages)
    messages.append(response)

    if not response.tool_calls:
        break  # done — subagent said its final words

    for tc in response.tool_calls:
        # ... execute tool, append result
```

**Why `range(30)`?** Without a limit, a confused subagent could loop forever. 30 turns is generous but bounded. Same loop pattern as the main agent — `invoke → check tool_calls → execute → repeat`.

### 5. Summary extraction — only the return value

```python
result = messages[-1].content  # last message = final answer
# fallback: search backwards for any non-ToolMessage with content
```

**Why the fallback?** Sometimes the last message is a ToolMessage (the model made a tool call as its final action but then the loop ended). Walk backwards to find the actual text response.

### 6. The `run_task` tool — the bridge

```python
@tool
def run_task(description: str) -> str:
    """Launch a subagent to handle a complex subtask."""
    return spawn_subagent(description)
```

From the main agent's perspective, `run_task` is just another tool. It sends a string, gets a string back. It has no idea a whole separate agent loop happened inside.

---

## The SimpleNamespace adapter — why it exists

```python
block = SimpleNamespace(name=tc["name"], input=tc["args"], id=tc["id"])
```

LangChain tool calls are plain dicts: `{"name": "run_bash", "args": {...}, "id": "..."}`.
The hook system expects objects with `.name`, `.input`, `.id` attributes (like Anthropic SDK blocks).
`SimpleNamespace` wraps the dict as an object so hooks work without rewriting them.

**It's a shim.** Bridges two APIs. One line instead of rewriting the whole hook system.

---

## Hook system applies to subagents too

```python
# Inside spawn_subagent:
blocked = trigger_hooks("PreToolUse", block)
```

The subagent goes through the **same permission hooks** as the main agent. If `run_bash("rm -rf /")` is blocked for the main agent, it's blocked for the subagent too.

**Why this matters:** A subagent is not an escape hatch. Same security rules apply. The main agent can't bypass permissions by delegating dangerous commands to a subagent.

---

## What's Missing for Production

### 1. No concurrency — subagents run one at a time

```python
# Current: blocks the main agent until subagent finishes
result = spawn_subagent(description)
```

**Problem:** Main agent spawns 3 subagents for 3 independent files → runs sequentially → 3× slower than needed.

**Production fix:** `asyncio.gather()` or thread pools. Spawn multiple subagents in parallel.

```python
# Production approach
results = await asyncio.gather(
    spawn_subagent("refactor foo.py"),
    spawn_subagent("refactor bar.py"),
    spawn_subagent("refactor baz.py"),
)
```

### 2. No resource limits — subagent can burn infinite tokens

**Problem:** Subagent loop has 30 turns max, but each turn could generate 4000 tokens. 30 × 4000 = 120K output tokens per subagent. No budget enforcement.

**Production fix:** Track token usage per subagent. Kill it if it exceeds a budget.

```python
# Production approach
token_budget = 50_000
tokens_used = 0
for _ in range(30):
    response = sub_llm.invoke(messages)
    tokens_used += response.usage_metadata.get("total_tokens", 0)
    if tokens_used > token_budget:
        return "Subagent terminated: token budget exceeded"
```

### 3. No timeout — a hung subagent blocks forever

**Problem:** If the LLM API hangs or a tool call takes 10 minutes, the whole program freezes.

**Production fix:** Wrap `spawn_subagent` in a timeout.

```python
import asyncio

async def spawn_subagent_with_timeout(desc, timeout=120):
    return await asyncio.wait_for(spawn_subagent(desc), timeout=timeout)
```

### 4. No result validation — main agent trusts subagent blindly

**Problem:** Subagent says "Done, refactored foo.py" but actually broke it. Main agent accepts the summary and moves on.

**Production fix:** Verify subagent output. Run tests. Check diffs. Don't trust the summary — trust the evidence.

```python
result = spawn_subagent("refactor foo.py")
# Verify: did the file actually change? Do tests still pass?
verification = run_bash.invoke({"command": "python -m pytest tests/"})
```

### 5. No state sharing — subagent can't see main agent's plan

**Problem:** Main agent has a todo list. Subagent doesn't know about it. Can't update it. Can't see what other subagents are doing.

**Production fix:** Shared state store (Redis, database, or in-memory dict with locks).

### 6. No error propagation — subagent failures are silent

**Problem:** If a subagent crashes or returns garbage, `run_task` returns it as a normal string. Main agent can't distinguish success from failure.

**Production fix:** Structured return with status codes.

```python
# Instead of returning raw string:
return {"status": "success", "summary": result, "tools_used": 12, "tokens": 8500}
# or
return {"status": "error", "error": "Token budget exceeded after 15 turns"}
```

### 7. No observability — what happened inside the subagent?

**Problem:** In production, you need to debug why a subagent made a bad edit. But its message history is discarded after it returns.

**Production fix:** Log the full subagent conversation to a trace store (e.g., LangSmith, OpenTelemetry, or a simple JSON log file).

```python
# After subagent finishes:
trace = {"task": description, "messages": serialize(messages), "result": result}
log_to_trace_store(trace)
```

### 8. Global mutable state — no isolation between sessions

**Problem:** `CURRENT_TODOS`, `HOOKS`, `TOOL_HANDLERS` are all module-level globals. Two users running the same server = shared state = data leakage.

**Production fix:** Session objects. Each user gets their own state.

```python
class AgentSession:
    def __init__(self):
        self.todos = []
        self.messages = []
        self.hooks = {"PreToolUse": [], "PostToolUse": [], ...}
```

---

## Summary table

| Feature | This code | Production |
|---------|-----------|------------|
| Recursion prevention | ✅ No `run_task` in subagent | ✅ Same |
| Fresh context | ✅ New messages[] | ✅ Same |
| Permission hooks | ✅ Shared with parent | ✅ Same |
| Concurrency | ❌ Sequential | Parallel with asyncio |
| Token budget | ❌ None | Per-subagent budget |
| Timeout | ❌ None | asyncio.wait_for |
| Result validation | ❌ Trust summary | Verify with tests/diffs |
| Error handling | ❌ Silent | Structured status codes |
| Observability | ❌ Print only | Trace store / LangSmith |
| State isolation | ❌ Globals | Session objects |

---

## Key Takeaway

> A subagent is a **fresh agent with a limited toolset and isolated context**.
> It exists to protect the main agent's memory from subtask noise,
> and to prevent recursion by removing the delegation tool.
>
> The prototype gets the **pattern** right: fresh context, limited tools, summary return.
> Production gets the **operational details** right: budgets, timeouts, observability, isolation.
>
> The pattern is the same one used by Claude Code, ChatGPT, and every serious agent framework:
> **delegate down, summarize up, never trust blindly.**
