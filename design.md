# Concurrency fix agent: design

Goal: given a C++ program with a data race or deadlock, the agent finds the bug from
ThreadSanitizer output, fixes it with minimal changes, and proves the fix with repeated runs.

---

## 1. Tools

### run_tsan
- **Input:** `file` (the .cpp to build and run). Nothing else: build flags, optimization
  level, number of runs and timeout all come from a config file the agent cannot see or change.
- **What it does:**
  1. Builds in Docker (Linux, `-fsanitize=thread -g`, optimization level from config).
  2. Runs the program N times (N from config), each run under a timeout.
  3. Preprocesses every report before returning it.
- **Output: one status**
  - `build_failed`: compiler errors (trimmed to the relevant lines).
  - `timeout`: the program hung (likely deadlock) + any partial report, preprocessed.
  - `warnings`: preprocessed report(s).
  - `crashed`: no warnings, but a run crashed or exited non-zero + project frames of the crash.
    Never counted as clean.
  - `clean`: no warnings and exit code 0 in all N runs + the program's output.
  - `infra_error`: Docker missing or stuck; not the agent's fault, stop the loop.
- **Preprocessing (inside the tool, the model never sees raw reports):** keep only
  - warning type (`data race`, `lock-order-inversion`, ...)
  - for each conflicting access: read/write, thread, and the **first stack frame whose file
    path is inside the project folder** (e.g. starts with `/work/`)
  - the variable from the `Location is ...` line
  - thread creation lines inside the project
  - drop all library frames (`/usr/...`, `libtsan`, `libstdc++`) and the SUMMARY line
    (it can point to library code for deadlocks)
  - de-duplicate identical warnings across runs

### read_file
- **Input:** `file`, optional `line`, optional `window` (lines above and below).
- **Output, decided by file size** (threshold in config, e.g. 300 lines):
  - **Small file:** the **whole file** with line numbers. Concurrency bugs are spread
    across a file (the shared variable, every function touching it, thread creation,
    existing mutexes), and small files are cheap to read in one call.
  - **Large file:** a window around `line` (default 20 lines each side; the model can ask
    for more), plus a note to use `search_code` for related places.
- **On failure:** `{"error": "..."}` (file not found, line out of range).
- Design note: started with windowed reads to save tokens; switched to size-based reads
  because a narrow window misses the file-wide context concurrency fixes need.

### search_code
- **Input:** `keyword`, `window`.
- **Output:** each match with surrounding lines and line numbers; capped number of matches
  with a note if there are more.

### edit_file
- **Input:** `file`, `old_text`, `new_text`.
- **Output:** success + a diff of the change, or an error.
- **Fails safely if** `old_text` is not found, or is found more than once.
  No line numbers: they shift after the first edit.

---

## 2. How the agent edits code
- Reading and editing are separate decisions: read the whole file when it's small,
  but always edit only the changed lines.
- Only the changed lines (old text → new text), never whole-file rewrites.
- Why: fewer tokens, lower latency, and no accidental changes elsewhere in the file.

---

## 3. The loop
```
agent → run_tsan
   ├─ clean (on the first run) → nothing to fix, report and stop
   ├─ build_failed → read errors → read_file → edit_file → run_tsan again
   ├─ timeout      → treat as deadlock → investigate lock order
   └─ warnings     → answer the 4 questions:
                       1. What kind of bug?
                       2. Where? (project-code frames)
                       3. Which variable?
                       4. Which threads?
                     → read_file / search_code → choose a fix → edit_file → run_tsan again
repeat until success or the limit is hit
```
- Decision: the agent calls `run_tsan` itself as its first step (one extra model call,
  in exchange for a uniform loop). Could compare against pre-running it in Week 5.

### Choosing a fix
| Situation | Fix |
|---|---|
| One simple shared variable | `std::atomic` |
| Several variables that must change together | `std::mutex` + `std::lock_guard` around all of them |
| `lock-order-inversion` / deadlock | consistent lock order, or `std::scoped_lock(m1, m2)` |

---

## 4. When to stop

### Success means ALL of these
1. Builds without errors.
2. Zero ThreadSanitizer warnings across **all N runs** (one clean run proves nothing).
3. No timeout in any run (the program finishes).
4. Output is correct: matches the original bug-free program's output where known,
   and is consistent across runs.
5. Passes every check in section 5.

### How many runs (N)
- Measured, not guessed: in Week 2, record how often ThreadSanitizer catches each bug in a
  single run. Pick N so the chance of missing a bug is negligible
  (e.g. 50% detection per run → 10 runs miss it ~0.1% of the time).

### Give up when
- Max attempts reached (in config), or a token/cost limit is reached.

### When it gives up, it reports
- What it found (bug type, location, variable), every fix it tried and why each failed,
  the last ThreadSanitizer output, and "needs manual review".

---

## 5. Never allowed, and how the code detects it

Checks are plain code first (cheap, fast, can't be argued with). An LLM verifier is an
optional second layer for subtle cases.

| Cheat | Detection |
|---|---|
| Delete or edit tests | Agent can only edit the target source files; test files are read-only |
| Remove a thread | Diff deletes a line containing `std::thread` → reject |
| Serialize threads (join before starting the next) | Timing check: much slower than the original → flag |
| One big lock around everything | Timing check + how many lines the new lock covers |
| Disable ThreadSanitizer in code | Diff contains `no_sanitize`, `suppress`, `TSAN`, `__tsan` → reject |
| Change build flags / optimization level | Build config owned by our code; agent has no tool to edit it |

### Where an LLM verifier helps (optional, compare in Week 4–5)
- Subtle wrong fixes code checks can't see, e.g. making two related variables atomic
  separately: ThreadSanitizer goes quiet but the logic is still broken.

---

## 6. Rules
- Change as few lines as possible.
- Max attempts and a cost limit always apply.
- The final report is detailed: bugs found, fixes applied, checks passed, anything that failed.
- Tool errors go back to the model as `{"error": ...}`; never crash the loop.

---

## 7. Lessons from Day 3
- A race can produce correct-looking output; only ThreadSanitizer revealed it.
- ThreadSanitizer on macOS is unreliable → always run in Linux Docker.
- No warning ≠ no race: ThreadSanitizer only sees what actually runs → repeat runs.
- Our code is not always frame `#0` (it was `#3` for the deadlock) → use the first
  project-path frame.
- The SUMMARY line can point to library code → don't rely on it.
- Line numbers drift with optimization (race reported on the `for` line, not `counter++`)
  → read a window of lines, and rely on the variable name.
- A bug can have several valid fixes → verify the problem is gone, don't compare
  against one expected fix.
- Reports are ~90% noise → preprocess before the model sees them.


Later: build with -Wall -Wextra, compare warnings with the original, report new ones as a fix-quality metric.