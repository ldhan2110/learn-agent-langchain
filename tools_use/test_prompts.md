# Test Prompts for main_concurrent.py

Run: `python main_concurrent.py`

## 1. Basic — Single tool call
```
What files are in the current directory?
```
Expect: single `run_bash(ls)` or `run_glob(*)`. Yellow `→` serial.

## 2. Concurrent reads — Multiple reads in parallel
```
Read main.py and main_concurrent.py, then tell me the key differences.
```
Expect: two `run_read` calls with green `⟂` (parallel).

## 3. Mixed concurrent + serial
```
Find all .py files, read each one, then create a file called summary.txt with a one-line description of each.
```
Expect: `run_glob` green ⟂ → `run_read` (multiple, green ⟂) → `run_write` yellow →.

## 4. Serial writes — Must not parallelize
```
Create three files: hello.txt with "hello", world.txt with "world", and combined.txt with "hello world".
```
Expect: three `run_write` calls, all yellow `→` serial.

## 5. Error recovery — Tool fails, loop continues
```
Read a file called nonexistent_file_12345.py, then list the current directory.
```
Expect: `run_read` returns error string, LLM continues with next action instead of crashing.

## 6. Bash concurrency classification
```
Show me git status and git log --oneline -5 at the same time.
```
Expect: both `run_bash` with read-only commands → green ⟂ parallel.

## 7. Edit tool — Serial
```
Read main_concurrent.py, then change MAX_PRINT_OUTPUT from 200 to 300.
```
Expect: `run_read` first, then `run_edit` yellow → serial. Verify file changed.

## 8. Path escape — Security check
```
Read the file ../../etc/passwd
```
Expect: `safe_path` blocks it with "Path escapes workspace" error.

## 9. Dangerous command — Safety check
```
Run: rm -rf /
```
Expect: "Dangerous command blocked" — no execution.

## 10. Multi-step chain — Full loop test
```
Create a Python file called fizzbuzz.py that prints fizzbuzz for 1-100, then run it and show me the output.
```
Expect: `run_write` → `run_bash(python fizzbuzz.py)` → text response. Multiple loop iterations.
