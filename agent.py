"""Concurrency fix agent: find a data race or deadlock with ThreadSanitizer, fix it, prove it.

Usage:  .venv/bin/python agent.py targets/race.cpp
Each run works on a copy in runs/<timestamp>-<name>/work/, so targets/ is never changed.
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime
from difflib import unified_diff
from pathlib import Path

import anthropic

import tools

# --- settings (later: move to a locked config file) ---
MODEL = "claude-sonnet-5-5"
EFFORT = "medium"            # Sonnet 5.5: medium is the starting point for agentic coding
MAX_TOKENS = 16000           # per model response
MAX_TURNS = 30               # model calls per run
MAX_TSAN_CALLS = 8           # run_tsan calls the model may make per run
MAX_COST_USD = 1.00          # stop before spending more than this per run
MAX_VERIFY_RETRIES = 2       # times the harness sends a failed verification back to the model
VERIFY_RUNS = 10             # runs in the harness's own final check (model's checks use tools.NUM_RUNS)
# Sonnet 5.5 prices, $ per million tokens (cache write = 1.25x input)
PRICE = {"input": 2.00, "output": 10.00, "cache_write": 2.50, "cache_read": 0.10}

# Anti-cheat: code that switches ThreadSanitizer off. Any of these appearing in an edit is rejected.
BANNED_PATTERNS = ["no_sanitize", "__tsan", "TSAN", "suppress", "disable_sanitizer",
                   "__has_feature(thread_sanitizer)", "__SANITIZE_THREAD__"]

SYSTEM_PROMPT = """You fix concurrency bugs (data races and deadlocks) in C++ programs.

You have four tools. run_tsan builds the file with ThreadSanitizer in Linux and runs it several
times; it returns build_failed, timeout (the program hung, usually a deadlock), warnings
(preprocessed ThreadSanitizer reports), crashed, or clean. read_file, search_code and edit_file
work on the project's source.

How to work:
1. Call run_tsan first to see what is wrong. If it is clean, say so and stop without editing.
2. For each warning, work out: what kind of bug, where (the project lines in the report), which
   variable or mutexes, and which threads. Read the code before deciding on a fix: the right fix
   depends on how the shared state is used everywhere, not just on the reported line.
3. Choose the smallest correct fix:
   - one independent shared variable: std::atomic
   - several variables that must change together, containers, or check-then-act: one std::mutex
     with std::lock_guard around every access to all of them (reads too)
   - lock-order inversion or deadlock: take the locks in one consistent order, or std::scoped_lock
4. Edit only the lines that need to change, then call run_tsan again. Repeat until it is clean.

Rules: never remove or merge threads, never join a thread before starting the next one to avoid
the race, never put one big lock around whole thread bodies, never disable or suppress
ThreadSanitizer, and keep the program's behaviour and output the same. If the program crashes
for a reason that is not a concurrency bug, report that and stop.

When you are done, reply with a short report: the bug (type, location, variable), the fix and
why it is correct."""

TOOL_SCHEMAS = [
    {"name": "run_tsan",
     "description": "Build the file with ThreadSanitizer and run it several times. Returns one "
                    "status: build_failed, timeout, warnings, crashed or clean, with details.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["file"],
                      "properties": {"file": {"type": "string", "description": "e.g. race.cpp"}}}},
    {"name": "read_file",
     "description": "Read a source file with line numbers. Small files come back whole; large "
                    "files come back as a window around `line`.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["file"],
                      "properties": {"file": {"type": "string"},
                                     "line": {"type": "integer", "description": "centre of the window (large files)"},
                                     "window": {"type": "integer", "description": "lines above and below"}}}},
    {"name": "search_code",
     "description": "Find a plain-text keyword in every source file, with surrounding lines. "
                    "Use it to find all uses of a variable or mutex.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["keyword"],
                      "properties": {"keyword": {"type": "string"},
                                     "window": {"type": "integer", "description": "lines of context, default 2"}}}},
    {"name": "edit_file",
     "description": "Replace exactly one occurrence of old_text with new_text. Copy old_text "
                    "exactly from read_file output, without the line-number prefix. Include "
                    "enough lines to make it unique. Returns a diff.",
     "input_schema": {"type": "object", "additionalProperties": False,
                      "required": ["file", "old_text", "new_text"],
                      "properties": {"file": {"type": "string"}, "old_text": {"type": "string"},
                                     "new_text": {"type": "string"}}}},
]


def load_env_file(path=".env"):
    """Read KEY=VALUE lines from .env into the environment (without overriding what's set)."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Run:
    """One attempt to fix one target: the working copy, the limits, the log and the report."""

    def __init__(self, target, runs_dir="runs"):
        self.target = Path(target)
        self.file = self.target.name
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = Path(runs_dir) / f"{stamp}-{self.target.stem}"
        self.dir, n = base, 1
        while self.dir.exists():                 # two runs in the same second
            n += 1
            self.dir = base.with_name(f"{base.name}-{n}")
        self.work = self.dir / "work"            # the only folder the tools can see
        self.work.mkdir(parents=True)
        shutil.copy(self.target, self.work / self.file)
        self.original = self.target.read_text()
        self.log_path = self.dir / "log.jsonl"
        self.turns = 0
        self.tsan_calls = 0
        self.verify_failures = 0
        self.cost = 0.0
        self.tokens = {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}
        self.rejected_edits = 0
        self.started = time.time()

    # --- logging ---
    def log(self, event, **data):
        with self.log_path.open("a") as f:
            f.write(json.dumps({"t": round(time.time() - self.started, 2), "event": event, **data},
                               default=str) + "\n")

    # --- cost ---
    def add_usage(self, usage):
        counts = {"input": usage.input_tokens or 0,
                  "output": usage.output_tokens or 0,
                  "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
                  "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0}
        for k, v in counts.items():
            self.tokens[k] += v
            self.cost += v * PRICE[k] / 1_000_000
        return counts

    def current_code(self):
        return (self.work / self.file).read_text()

    def diff(self):
        return "\n".join(unified_diff(self.original.splitlines(), self.current_code().splitlines(),
                                      fromfile=f"original/{self.file}", tofile=f"fixed/{self.file}",
                                      lineterm=""))

    # --- anti-cheat: checked before an edit is applied ---
    def check_edit(self, file, old_text, new_text):
        if file != self.file:
            return f"only {self.file} may be edited"
        before = self.current_code()
        if before.count(old_text) != 1:
            return None                      # let edit_file report not-found / ambiguous
        after = before.replace(old_text, new_text, 1)
        if after.count("std::thread") < self.original.count("std::thread"):
            return "the edit removes a std::thread; threads must not be removed or merged"
        for pattern in BANNED_PATTERNS:
            if pattern in after and pattern not in self.original:
                return f"the edit adds '{pattern}'; ThreadSanitizer must not be disabled or suppressed"
        return None

    # --- tool dispatch ---
    def call_tool(self, name, args):
        """Run one tool call from the model. Returns (result dict, stop reason or None)."""
        try:
            if name == "run_tsan":
                self.tsan_calls += 1
                if self.tsan_calls > MAX_TSAN_CALLS:
                    return {"error": f"run_tsan limit ({MAX_TSAN_CALLS}) reached"}, "tsan_limit"
                result = tools.run_tsan(self.work, args["file"])
                if result.get("status") == "infra_error":
                    return result, "infra_error"
                return result, None
            if name == "read_file":
                return tools.read_file(self.work, args["file"], args.get("line"), args.get("window")), None
            if name == "search_code":
                return tools.search_code(self.work, args["keyword"], args.get("window", 2)), None
            if name == "edit_file":
                reason = self.check_edit(args["file"], args["old_text"], args["new_text"])
                if reason:
                    self.rejected_edits += 1
                    return {"error": f"edit rejected: {reason}"}, None
                return tools.edit_file(self.work, args["file"], args["old_text"], args["new_text"]), None
            return {"error": f"unknown tool: {name}"}, None
        except KeyError as e:
            return {"error": f"missing argument {e} for {name}"}, None
        except Exception as e:                # a tool bug must never crash the loop
            return {"error": f"{name} failed: {type(e).__name__}: {e}"}, None

    # --- final check, done by the harness, not the model ---
    def verify(self):
        result = tools.run_tsan(self.work, self.file, num_runs=VERIFY_RUNS)
        ok = result.get("status") == "clean" and result.get("consistent_output", False)
        return ok, result


def call_model(client, messages):
    return client.beta.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=SYSTEM_PROMPT,
        tools=TOOL_SCHEMAS,
        messages=messages,
        output_config={"effort": EFFORT},
        cache_control={"type": "ephemeral"},       # each turn re-sends the history: cache it
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",                       # if Sonnet 5.5 declines, retry on a fallback model
    )


def block_to_dict(block):
    if hasattr(block, "to_dict"):
        return block.to_dict()
    return dict(vars(block)) if hasattr(block, "__dict__") else block


def run_agent(target, client=None, runs_dir="runs", verbose=True):
    run = Run(target, runs_dir)
    say = print if verbose else (lambda *a, **k: None)
    say(f"run folder: {run.dir}")
    run.log("start", target=str(target), model=MODEL, effort=EFFORT,
            limits={"turns": MAX_TURNS, "tsan_calls": MAX_TSAN_CALLS, "cost_usd": MAX_COST_USD})

    if client is None:
        load_env_file()
        client = anthropic.Anthropic()

    messages = [{"role": "user", "content":
                 f"Find and fix the concurrency bug in {run.file}. Start by calling run_tsan."}]
    outcome, stop_reason, final_text, verification = None, None, "", None

    while outcome is None:
        if run.turns >= MAX_TURNS:
            outcome, stop_reason = "gave_up", f"turn limit ({MAX_TURNS})"
            break
        if run.cost >= MAX_COST_USD:
            outcome, stop_reason = "gave_up", f"cost limit (${MAX_COST_USD:.2f})"
            break

        run.turns += 1
        try:
            response = call_model(client, messages)
        except anthropic.APIError as e:
            outcome, stop_reason = "error", f"API error: {type(e).__name__}: {e}"
            break
        usage = run.add_usage(response.usage)
        run.log("model", turn=run.turns, stop_reason=response.stop_reason, usage=usage,
                cost_so_far=round(run.cost, 4), content=[block_to_dict(b) for b in response.content])
        messages.append({"role": "assistant", "content": response.content})

        texts = [b.text for b in response.content if b.type == "text" and b.text.strip()]
        for t in texts:
            say(f"\n[model] {t}")
        tool_uses = [b for b in response.content if b.type == "tool_use"]

        if response.stop_reason == "refusal":
            outcome, stop_reason = "error", "model refused"
            break

        if tool_uses:
            results, halt = [], None
            for tu in tool_uses:
                say(f"[tool] {tu.name} {json.dumps(tu.input)[:120]}")
                result, tool_halt = run.call_tool(tu.name, tu.input)
                run.log("tool", turn=run.turns, name=tu.name, input=tu.input, result=result)
                summary = result.get("status") or result.get("error") or "ok"
                say(f"       -> {str(summary)[:120]}")
                results.append({"type": "tool_result", "tool_use_id": tu.id,
                                "content": json.dumps(result),
                                "is_error": "error" in result})
                halt = halt or tool_halt
            messages.append({"role": "user", "content": results})
            if halt == "infra_error":
                outcome, stop_reason = "error", "infrastructure error (Docker)"
            elif halt == "tsan_limit":
                outcome, stop_reason = "gave_up", f"run_tsan limit ({MAX_TSAN_CALLS})"
            continue

        # No tool calls: the model says it's done. Check it ourselves.
        final_text = "\n".join(texts)
        changed = run.current_code() != run.original
        say(f"\n[harness] verifying with {VERIFY_RUNS} runs...")
        ok, verification = run.verify()
        run.log("verify", ok=ok, changed=changed, result=verification)
        if ok:
            outcome = "fixed" if changed else "no_bug_found"
            break
        if not changed and verification.get("status") == "crashed":
            outcome, stop_reason = "not_concurrency", "program crashes without a concurrency warning"
            break
        run.verify_failures += 1
        if run.verify_failures > MAX_VERIFY_RETRIES:
            outcome, stop_reason = "gave_up", "verification kept failing"
            break
        say(f"[harness] verification failed ({verification.get('status')}); sending back to model")
        messages.append({"role": "user", "content":
                         "Independent verification with more runs did not pass, so the bug is not "
                         f"fixed yet:\n{json.dumps(verification)}\nKeep working."})

    report = {
        "target": str(target), "outcome": outcome, "stop_reason": stop_reason,
        "model": MODEL, "effort": EFFORT,
        "turns": run.turns, "run_tsan_calls": run.tsan_calls, "rejected_edits": run.rejected_edits,
        "tokens": run.tokens, "cost_usd": round(run.cost, 4),
        "seconds": round(time.time() - run.started, 1),
        "verification": verification, "model_report": final_text, "diff": run.diff(),
        "run_folder": str(run.dir),
    }
    if outcome not in ("fixed", "no_bug_found"):
        report["needs_manual_review"] = True
        report["last_tsan_output"] = next(
            (json.loads(r["content"]) for m in reversed(messages) if m["role"] == "user"
             and isinstance(m["content"], list) for r in m["content"]
             if r.get("type") == "tool_result" and '"status"' in r["content"]), None)
    (run.dir / "report.json").write_text(json.dumps(report, indent=2, default=str))
    (run.dir / "fix.diff").write_text(report["diff"] + "\n")
    run.log("end", **{k: report[k] for k in ("outcome", "stop_reason", "turns", "cost_usd")})

    say(f"\n=== {outcome.upper()} ===" + (f" ({stop_reason})" if stop_reason else ""))
    say(f"turns {run.turns}, run_tsan calls {run.tsan_calls}, rejected edits {run.rejected_edits}, "
        f"cost ${run.cost:.4f}, {report['seconds']}s")
    if report["diff"]:
        say(report["diff"])
    say(f"report: {run.dir / 'report.json'}")
    return report


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python agent.py <target.cpp>")
    result = run_agent(sys.argv[1])
    sys.exit(0 if result["outcome"] in ("fixed", "no_bug_found") else 1)
