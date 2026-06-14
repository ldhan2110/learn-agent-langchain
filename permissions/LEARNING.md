# Permission Systems for AI Agents — Learning Notes

## Two files, same agent, different maturity

| File | Level | Key idea |
|------|-------|----------|
| `main.py` | Prototype | 3-gate pipeline with substring matching |
| `main_production.py` | Production | Token parsing, audit logs, external config, rate limits |

---

## The 3-Gate Pipeline (both files share this pattern)

```
Tool Call → Gate 1 (hard deny) → Gate 2 (context rules) → Gate 3 (user prompt) → Execute
```

This is the core insight: **permission checks are a pipeline, not a single function**.
Each gate has a different job and different cost.

---

## What changed from prototype → production (and why)

### 1. Substring matching → Token parsing (`shlex.split`)

**Problem:** `"sudo" in command` matches `"explain pseudocode"`.

**Solution:** Parse command into tokens, check the *binary name* and *flags* separately.

```python
# Prototype (main.py) — string contains
"sudo" in "explain pseudocode"  # True! False positive.

# Production (main_production.py) — token parse
shlex.split("explain pseudocode")  # ["explain", "pseudocode"]
Path("explain").name  # "explain" — not in dangerous_binaries
```

**Lesson:** Never match security patterns with substring. Parse the structure.

### 2. Hardcoded rules → External YAML config

**Problem:** Changing a rule means editing Python code and redeploying.

**Solution:** `permissions.yaml` — edit rules without touching code.

```yaml
# Add a new blocked binary: just add a line
dangerous_binaries:
  - sudo
  - su
  - newdangertool   # ← no code change needed
```

**Lesson:** Security rules change faster than code. Separate them.

### 3. No logging → Structured audit trail

**Problem:** Something bad happened. Who did what? When? You have no idea.

**Solution:** Every decision logged as JSON to `audit.log`.

```json
{"ts": "2026-06-14T10:00:00Z", "tool": "run_bash", "args": {"command": "rm -rf /"}, "decision": "deny", "reason": "Blocked: contains 'rm -rf /'"}
```

**Lesson:** In production, "print to console" is not logging.
Use structured JSON so you can `grep`/`jq` it later.

### 4. No rate limiting → Sliding window per tool

**Problem:** Model calls `run_bash` 100 times in 10 seconds trying to bypass blocks.

**Solution:** Sliding window counter. After N calls in M seconds → hard deny.

```python
# 30 bash calls per 60 seconds max
rate_limits:
  run_bash:
    max_calls: 30
    window_seconds: 60
```

**Lesson:** Rate limiting is defense against both bugs (infinite loops) and attacks (brute force).

### 5. Binary allow/deny → Tiered decisions (ALLOW/PROMPT/DENY)

**Problem:** Some things are clearly dangerous (deny), some clearly safe (allow), but many are *ambiguous* (maybe dangerous, maybe not).

**Solution:** Three tiers:
- `DENY` — hard block, no prompt (e.g., `sudo`, `rm -rf /`)
- `PROMPT` — ask user y/N (e.g., `rm file.txt`, `curl`)  
- `ALLOW` — pass through silently (e.g., `ls`, `cat`)

**Lesson:** Not everything is black/white. The middle tier (PROMPT) handles the gray zone.

---

## Gate Ordering — Why It Matters

```
Rate Limit → Command Parse → Path Escape → User Prompt
   (cheap)     (medium)       (medium)       (expensive)
```

- **Cheapest checks first** — rate limit is O(1), no parsing needed
- **Hard denies before soft prompts** — don't bother asking user about `rm -rf /`
- **User prompt is last resort** — only for genuinely ambiguous cases

This is a general pattern: **filter pipeline, cheapest first, most certain first**.

---

## Testing Comparison

### main.py (prototype)
```
agent >> explain pseudocode      # BUG: blocked (substring "sudo")
agent >> su -c 'rm -rf /'       # BUG: passes (no "sudo" match)
```

### main_production.py (production)
```
agent >> explain pseudocode      # PASS: "explain" not in dangerous_binaries
agent >> su -c 'rm -rf /'       # DENY: "su" is a dangerous binary
```

---

## What's Still Missing (next level)

| Feature | Why |
|---------|-----|
| **Sandboxing** | Permission checks are advisory. Real isolation = Docker/gVisor/Firecracker |
| **LLM-aware rules** | Model might encode payloads in base64, split across multiple calls |
| **Session memory** | "User allowed `rm` once" doesn't mean "allow `rm` forever" |
| **Async approval** | In production, "input()" blocks the event loop. Use async queues |
| **Multi-user** | Different users need different permission levels |

---

## Key Takeaway

> A permission system is a **pipeline of increasingly expensive checks**.
> Each gate answers one question. Together they cover the spectrum
> from "obviously dangerous" to "probably fine but let's ask."
>
> The prototype gets the **shape** right. Production gets the **details** right.
> Both use the same pattern — that's what makes the pattern worth learning.
