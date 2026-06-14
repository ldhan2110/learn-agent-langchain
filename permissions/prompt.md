# Testing the Permission Gates

Run: `python main.py`

## Gate 1: Hard Deny List (always blocked, no prompt)

```
agent >> delete everything with rm -rf /
agent >> run sudo apt-get install something
agent >> use dd if=/dev/zero to wipe disk
agent >> format disk with mkfs
```

Expected: Red `⛔ Blocked` message. No user prompt. Tool returns "Permission denied."

## Gate 2: Rule Matching → User Approval (yellow warning, asks y/N)

### Destructive bash commands (triggers `rm `, `> /etc/`, `chmod 777`)

```
agent >> remove the file test.txt using rm command
agent >> write something to > /etc/hosts
agent >> make everything writable with chmod 777
```

Expected: Yellow `⚠ Potentially destructive command` → asks `Allow? [y/N]`. Try both `y` and `n`.

### Write/edit outside workspace (triggers path escape check)

This one is harder to trigger since the model would need to call `run_write` or `run_edit` with an absolute path outside WORKDIR. You can test the logic directly:

```python
# In a Python shell:
from main import check_rules
check_rules("run_write", {"path": "/etc/passwd"})   # → "Writing outside workspace"
check_rules("run_write", {"path": "hello.txt"})      # → None (allowed)
check_rules("run_edit", {"path": "/tmp/evil.txt"})   # → "Writing outside workspace"
```

## Gate 3: Normal operations (no gates triggered, runs freely)

```
agent >> list all python files in this directory
agent >> read the file main.py
agent >> what is 2+2, use bash to calculate
agent >> create a file called hello.txt with "hello world"
agent >> find all .md files
```

Expected: Tool runs directly, no warnings, no prompts.

## Edge Cases Worth Exploring

```
# Deny list substring matching — does "sudo" inside a word trigger it?
agent >> explain what pseudocode is

# Multiple tool calls — does each get checked independently?
agent >> read main.py and also list all files

# Permission denied flow — does the model recover gracefully?
agent >> delete all files using rm -rf /
# (after block, does model respond sensibly?)
```

## What to Watch For

1. Gate 1 blocks silently (no user prompt) — correct?
2. Gate 2 shows warning AND asks permission — correct?
3. Saying "n" at Gate 2 → model gets "Permission denied." and adjusts
4. Normal tools skip all gates — no unnecessary friction
5. The 3-gate pipeline: deny list → rule match → user approval → execute
