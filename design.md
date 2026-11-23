# Concurrency fix agent: design

Goal: given a C++ program with a data race or deadlock, the agent finds the bug from
ThreadSanitizer output, fixes it with minimal changes, and proves the fix with repeated runs.

---

## 1. Tools

### run_tsan
- **Input:** `file` (the .cpp to build and run). Nothing else: build flags, optimization
  level, number of runs and timeout all come from a config file the agent cannot see or change.
- **What it does:**
  1. Checks `file`: it comes from the model and goes into a shell command, so it must be an
     existing `.cpp/.cc/.cxx/.c` file inside the project folder, and it is shell-quoted.
  2. Builds in Docker (`gcc:14`, Linux, `-std=c++20 -fsanitize=thread -g -O0`, GCC errors
     as JSON). C++20 for `std::barrier` in the targets.
  3. Runs the program N times (N=5), each under a 10 s timeout, with
     `TSAN_OPTIONS=second_deadlock_stack=1` (lock-order reports then also show where the
     first lock was taken).
  4. Preprocesses every report before returning it. Program output sent to the model is
     capped at 2000 characters.
- The harness can ask for more runs and the full output (its own final check); the model can't.
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
    path is inside the project folder** (e.g. starts with `/work/`), **plus that source
    line**: lock-order reports never name the mutex, the line (`lock_guard b(m1)`) does
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
repeat until the model stops calling tools or a limit is hit
   → harness verifies (10 runs, expected output) → reviewer checks the diff
   → failure goes back to the model, max 2 times (section 4)
```
- Decision: the agent calls `run_tsan` itself as its first step (one extra model call,
  in exchange for a uniform loop). Could compare against pre-running it in Week 5.
- Implementation: our own tool-use loop on the Messages API (`agent.py`), model
  `claude-sonnet-5-5` at medium effort, prompt caching, server-side refusal fallback.
  Each run works on a copy in `runs/<timestamp>-<name>/work/`, so `targets/` never changes.
  The model can send several tool calls in one turn; they run in order.

### Choosing a fix
| Situation | Fix |
|---|---|
| One simple shared variable | `std::atomic` |
| Several variables that must change together, containers, check-then-act | one `std::mutex` + `std::lock_guard` around every access, reads too |
| `lock-order-inversion` / deadlock | consistent lock order, or `std::scoped_lock(m1, m2)` |
| Hang with no TSan report on a condition variable | wait with a predicate: `cv.wait(lock, [] { return ready; })` |

---

## 4. When to stop

### Success means ALL of these
1. Builds without errors.
2. Zero ThreadSanitizer warnings across **all N runs** (one clean run proves nothing).
3. No timeout in any run (the program finishes).
4. Output is correct: matches the original bug-free program's output where known,
   and is consistent across runs.
5. Passes every check in section 5, including the reviewer's approval.

### How many runs (N)
- Measured, not guessed: in Week 2, record how often ThreadSanitizer catches each bug in a
  single run. Pick N so the chance of missing a bug is negligible
  (e.g. 50% detection per run → 10 runs miss it ~0.1% of the time).

### Who decides success: the harness, never the model
When the model stops calling tools, the harness checks the result itself:
1. `run_tsan` with more runs than the model gets (10 vs 5): must be `clean`, same output every run.
2. The output must equal the target's `expected_output` (from `targets/manifest.json`).
3. If the file changed: the LLM reviewer (section 5) must approve the diff.

A failed check or a rejected review goes back to the model as feedback (at most 2 times),
then the run gives up. No edits + clean → `no_bug_found`; no edits + crash → `not_concurrency`.

### Give up when
- 30 model turns, 8 `run_tsan` calls, $1.00 spent, or 2 failed verifications/reviews
  (all per run; constants in `agent.py`, later a locked config file).

### When it gives up, it reports
- What it found (bug type, location, variable), every fix it tried and why each failed,
  the last ThreadSanitizer output, and "needs manual review".
- Every run writes `runs/<timestamp>-<name>/`: `log.jsonl` (every model turn and tool call),
  `report.json` (outcome, cost, verification, review, diff) and `fix.diff`.

---

## 5. Never allowed, and how the code detects it

Three layers, cheapest first. Each catches what the one before it misses.

**Layer 1: static checks before every edit** (`Run.check_edit` in `agent.py`). The edit is
simulated on the current file and compared with the original; a violation rejects the edit
and tells the model why, so it can choose a valid fix.

**Layer 2: concurrency-proof targets.** Benchmark targets split each thread's work into two
phases with a `std::barrier` between them, so all threads must be running at once. Running
them one after another, or holding one lock across a whole thread body, makes the program
hang → `timeout` → verification fails. The barrier lines are protected (layer 1).

**Layer 3: LLM reviewer on the final diff** (`review_fix`). A separate model call, after
TSan is clean and the output is right, sees original, fixed code, diff and the fixer's
report, and returns approve/reject + problems (JSON schema). Code and comments are treated
as material to review, never instructions.

| Cheat | Detection |
|---|---|
| Edit tests or other files | Only the target file can be edited; reference fixes and manifest are outside the agent's folder |
| Remove a thread | Fewer `std::thread` than the original → reject edit |
| Disable ThreadSanitizer in code | Edit adds `no_sanitize`, `suppress`, `TSAN`, `__tsan`, ... → reject edit |
| Change build flags / optimization level | Build config owned by our code; agent has no tool to edit it |
| Serialize threads (join before starting the next) | More `.join()` before the last thread start than the original → reject edit; barrier makes any other form hang |
| One big lock around a whole loop | Lock in a thread function whose next statement is a loop → reject edit; barrier hangs on other forms |
| Remove the barrier | Protected text from the manifest must not decrease → reject edit |
| Change the program's output | Verification compares with `expected_output` |
| Separate atomics on related variables, other subtle wrong fixes | Target exits non-zero when its invariant breaks (`crashed`); reviewer |

### Why there is no timing check
The first plan was "much slower than the original → serialized". Measured (2026-11-23), the
cheats are **5-25x faster**, not slower: in these programs the cost is contention on the
lock or atomic, and serializing removes it (race: atomic fix 54 ms, big lock 4 ms; multi_var:
per-transfer lock 107 ms, big lock 21 ms). "Suspiciously fast" is no good either: a better
fix (per-thread counts added once at the end) is fast too. So timing is not used at all.

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

---

## 8. Targets and benchmark
- `targets/*.cpp`: bug programs with neutral comments only (no hints for the agent).
  What each one tests is in `targets/README.md`.
- `targets/manifest.json`: per target, the bug, `expected_outcome` (`fixed` /
  `no_bug_found`), `expected_output`, the reference fix and protected text. Agent never sees it.
- `targets/reference/*.cpp`: a hand-written correct fix for each buggy target, checked clean.
- `bench.py`: runs every target K times (in parallel), prints success rate, cost, turns,
  rejected edits, failed verifications and review rejections per target, and saves
  `bench/<timestamp>.json`.
- First benchmark (2026-11-23, Sonnet 5.5 medium, review on, 6 targets x 3): **18/18**,
  $0.37 total (reviewer ~25% of it), 140 s. Every diff a real, minimal fix. These targets
  are too easy to separate good from great → harder ones next.

---

## 9. Lessons from building the PoC (2026-11-23)
- Measure a check before building it: the planned timing check would have caught nothing
  (cheats ran faster). Concurrency must be forced by the targets (barriers) instead.
- TSan reports lock-order inversion from the lock graph even if the threads never overlap,
  so serializing a lock-order bug does not hide it.
- A deadlocked program still prints its lock-order report before hanging → parse the
  report on `timeout` too.
- A crash or non-zero exit with no TSan warning must never count as clean.
- Hints in target comments leak the answer to the agent → keep targets neutral.
- Program output can be huge (2M lines) → cap what the model sees.
- Anything the model passes to a shell is untrusted input → validate and quote it.

---

Later:
- Build with -Wall -Wextra, compare warnings with the original, report new ones as a fix-quality metric.
- Harder targets: large file, race on a heap object/class member, partial locking,
  check-then-act, intermittent race. Pytest suite for tools, preprocessing and the loop.
- Measure TSan detection rate per target and pick N from it (section 4).
- Compare Sonnet vs Opus, effort levels, and agent-calls-run_tsan-first vs pre-run.
- Multi-file projects; a locked config file instead of constants in code.