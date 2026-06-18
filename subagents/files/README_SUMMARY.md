# Project Summary

## `subagents/files/main_hooks.py`
* Creates a sandboxed LangChain **coding agent** with custom hooks.
* Defines a set of core tools (`run_bash`, `run_read`, `run_write`, `run_edit`, `run_glob`).
* Implements a **hook system**:
  * `before_tool_call`
  * `after_tool_call`
  * `on_stop`
* Includes a **permission layer** – deny‑list, destructive‑command warning, and workspace‑escape protection.
* Provides a simple interactive prompt for debugging and user input.

## `subagents/files/main_permissions.py`
* Production‑grade **permission framework** for a LangChain coding agent.
* Utilizes `shlex` for token‑aware shell parsing.
* Defines three decision tiers: `ALLOW`, `PROMPT`, `DENY`.
* Supports **audit logging** and external YAML configuration (`permissions.yaml`) for rule changes.
* Adds **rate limiting** per tool and a path‑escape checker for file‑write operations.
* Pipelines multiple gates: rate limit → command parse → path escape → final decision.

## `subagents/files/main_todo.py`
* A demo/skeleton **TODO agent** – typically a minimal agent loop that processes pending tasks.
* Illustrates how to integrate a task list with LangChain.

## `subagents/files/main_concurrent.py`
* Shows handling of **concurrent tool calls**.
* Demonstrates use of Python’s async/await or threading to run multiple tools in parallel.

## `subagents/files/main_tooluse.py`
* Focuses on **tool‑use patterns**.
* Provides patterns for chaining tool calls, handling responses, and retry logic.

## `subagents/files/main_agent_loop.py`
* Implements the core agent loop logic.
* Orchestrates tool execution, handles “stop” signals, and integrates hooks/permissions.

---

### Summary
These files collectively provide a modular sandboxed language‑model coding agent:
* **Core capabilities** – running shell commands, reading/writing files, editing, globbing.
* **Safety** – hooks, permission checks, escaper protection.
* **Extensibility** – configuration, audit, concurrent call patterns.
* **Convenience** – skeletons for TODO processing and agent loops.

Let me know if you’d like deeper dives into specific code blocks or further documentation!