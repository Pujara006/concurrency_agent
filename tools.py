import difflib
import json
import re
import signal
import subprocess
from pathlib import Path

# --- settings (later: move to a locked config file) ---
DOCKER_IMAGE = "gcc:14"
BUILD_FLAGS = "-std=c++17 -fsanitize=thread -g -O0 -fdiagnostics-format=json"
BUILD_TIMEOUT = 60      # seconds for a build (later: measured per target)
NUM_RUNS = 5
RUN_TIMEOUT = 10        # seconds per run (later: measured per target)
MAX_ERRORS = 5          # compiler errors sent to the model
MAX_WARNINGS = 5        # distinct TSan warnings sent to the model
PROJECT_ROOT = "/work/" # frames under this path are project code
# second_deadlock_stack: lock-order reports also show where the first lock was taken
TSAN_OPTIONS = "second_deadlock_stack=1"


def run_in_docker(workdir, command, timeout=60):
    """Run a shell command inside a Linux container with `workdir` mounted at /work."""
    cmd = ["docker", "run", "--rm",
           "-v", f"{Path(workdir).resolve()}:/work", "-w", "/work",
           DOCKER_IMAGE, "bash", "-c", command]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except subprocess.TimeoutExpired:
        return {"exit_code": -1, "stdout": "", "stderr": "docker call timed out", "infra": True}
    except FileNotFoundError:
        return {"exit_code": -1, "stdout": "", "stderr": "docker not found", "infra": True}


def _read_line(workdir, file, line):
    """Return one source line (stripped), or '' if it can't be read."""
    try:
        lines = (Path(workdir) / file).read_text().splitlines()
        return lines[line - 1].strip() if 1 <= line <= len(lines) else ""
    except OSError:
        return ""


def parse_build_errors(workdir, stderr):
    """Turn GCC's JSON diagnostics into a short list of errors for the model."""
    try:
        diags = json.loads(stderr)
    except json.JSONDecodeError:
        # Not JSON (e.g. a linker error): fall back to plain-text error lines
        lines = [l for l in stderr.splitlines()
                 if "error" in l.lower() or "undefined reference" in l]
        return {"errors": [{"message": l} for l in lines[:MAX_ERRORS]],
                "omitted": max(0, len(lines) - MAX_ERRORS)}

    errors = []
    for d in diags:
        if d.get("kind") != "error":
            continue
        entry = {"message": d.get("message", "")}
        locs = d.get("locations") or []
        if locs and "caret" in locs[0]:
            caret = locs[0]["caret"]
            entry["file"] = caret.get("file")
            entry["line"] = caret.get("line")
            entry["code"] = _read_line(workdir, caret.get("file", ""), caret.get("line", 0))
        fixits = d.get("fixits") or []
        if fixits:
            f = fixits[0]
            entry["compiler_suggestion"] = (
                f"insert '{f.get('string', '')}' at line {f['start']['line']}, "
                f"column {f['start']['column']}")
        errors.append(entry)

    return {"errors": errors[:MAX_ERRORS], "omitted": max(0, len(errors) - MAX_ERRORS)}


# --- ThreadSanitizer report preprocessing ---
# A frame looks like: "#4 func1() /work/lockorder.cpp:11 (lockorder+0x400f98)".
# The function name can contain spaces, so the path is the last token before "(module+offset)".
_FRAME_RE = re.compile(r"^#\d+\s+(?P<func>.*?)\s+(?P<path>\S+?):(?P<line>\d+)(?::\d+)?\s+\([^)]*\)$")
_THREAD_RE = re.compile(r"\b(?:by|in) (main thread|thread T\d+)")


def _clean(text):
    """Drop addresses, pids, tids and module offsets: they differ between runs and mean nothing to the model."""
    text = re.sub(r"\s+at 0x[0-9a-f]+", "", text)
    text = re.sub(r"\s*\((?:0x[0-9a-f]+|tid=[^)]*|pid=\d+|[\w.\-]+\+0x[0-9a-f]+)\)", "", text)
    return text.rstrip(":").strip()


def _project_frame(workdir, frames):
    """First stack frame inside the project (library frames are skipped), as a compact dict."""
    for frame in frames:
        m = _FRAME_RE.match(frame)
        if m and m["path"].startswith(PROJECT_ROOT):
            rel = m["path"][len(PROJECT_ROOT):]
            line = int(m["line"])
            return {"at": f"{rel}:{line}", "function": m["func"], "code": _read_line(workdir, rel, line)}
    return {"at": None, "note": "no frame in project code"}


def _project_frames(workdir, lines, limit=5):
    """All project-code frames in a stack (e.g. a crash), innermost first."""
    frames = []
    for raw in lines:
        f = _project_frame(workdir, [raw.strip()])
        if f["at"]:
            frames.append(f)
    return frames[:limit]


def _split_sections(block):
    """Group a report's lines into (header, [frames]) pairs. Headers are indented 2 spaces, frames 4."""
    sections = []
    for raw in block.splitlines():
        line = raw.strip()
        if not line or line.startswith("Hint:") or line.startswith("SUMMARY:"):
            continue                          # SUMMARY can point at library code: never use it
        if line.startswith("#"):
            if sections:
                sections[-1][1].append(line)
        else:
            sections.append((line, []))
    return sections


def parse_tsan_report(workdir, block):
    """Turn one raw ThreadSanitizer warning into the few facts the model needs."""
    sections = _split_sections(block)
    header = next((h for h, _ in sections if h.startswith("WARNING: ThreadSanitizer:")), None)
    if header is None:
        return None
    warning = {"type": _clean(header.split("ThreadSanitizer:", 1)[1]), "events": [], "threads": []}

    for text, frames in sections:
        if text == header:
            continue
        if text.startswith("Location is "):
            warning["location"] = _clean(text[len("Location is "):])
            name = re.search(r"'([^']+)'", text)
            if name:
                warning["variable"] = name.group(1)
            if frames:                        # heap block: where it was allocated
                warning["allocated"] = _project_frame(workdir, frames)
        elif text.startswith("Cycle in lock order graph:"):
            warning["lock_cycle"] = _clean(text.split(":", 1)[1])
        elif text.startswith("Thread T") and "created by" in text:
            tid = re.match(r"Thread (T\d+)", text).group(1)
            created = _project_frame(workdir, frames)
            warning["threads"].append({"thread": tid, "created_at": created.get("at"),
                                       "code": created.get("code", "")})
        elif frames:                          # an access, lock acquisition, mutex creation, ...
            event = {"what": _clean(text)}
            thread = _THREAD_RE.search(text)
            if thread:
                event["thread"] = thread.group(1).replace("thread ", "")
            event.update(_project_frame(workdir, frames))
            warning["events"].append(event)
    return warning


def _dedup_key(w):
    """Same bug = same type, same variable, same project lines. Ignores thread ids and read/write
    order, which change from run to run for the same race."""
    return (w["type"], w.get("variable"), tuple(sorted({e.get("at") or "" for e in w["events"]})))


def summarize_reports(workdir, stderrs):
    """Parse the TSan warnings in each run's stderr, merge duplicates, cap the number returned."""
    merged = {}
    for run_index, stderr in enumerate(stderrs):
        for block in stderr.split("=================="):
            if "WARNING: ThreadSanitizer:" not in block:
                continue
            w = parse_tsan_report(workdir, block)
            if w is None:
                continue
            key = _dedup_key(w)
            if key not in merged:
                merged[key] = {**w, "occurrences": 0, "_runs": set()}
            merged[key]["occurrences"] += 1
            merged[key]["_runs"].add(run_index)

    warnings = []
    for w in merged.values():
        w["seen_in_runs"] = len(w.pop("_runs"))
        warnings.append(w)
    warnings.sort(key=lambda w: -w["occurrences"])
    return {"warnings": warnings[:MAX_WARNINGS], "omitted": max(0, len(warnings) - MAX_WARNINGS)}


def run_tsan(workdir, file):
    """Build `file` with ThreadSanitizer, run it NUM_RUNS times, return one status."""
    # 1. Build
    build = run_in_docker(workdir,
                          f"mkdir -p .build && g++ {BUILD_FLAGS} {file} -o .build/prog",
                          timeout=BUILD_TIMEOUT)
    if build.get("infra"):
        return {"status": "infra_error", "message": build["stderr"]}
    if build["exit_code"] != 0:
        return {"status": "build_failed", **parse_build_errors(workdir, build["stderr"])}

    # 2. Run N times, each with a timeout inside the container
    runs = []
    for i in range(NUM_RUNS):
        r = run_in_docker(workdir,
                          f"TSAN_OPTIONS={TSAN_OPTIONS} timeout {RUN_TIMEOUT} ./.build/prog",
                          timeout=RUN_TIMEOUT + 30)
        if r.get("infra"):
            return {"status": "infra_error", "message": r["stderr"]}
        if r["exit_code"] == 124:   # `timeout` killed the program
            # TSan prints lock-order reports when it sees the cycle, before the hang,
            # so a timed-out run often still explains itself
            return {"status": "timeout", "run": i + 1,
                    "message": f"program did not finish within {RUN_TIMEOUT}s (likely deadlock)",
                    **summarize_reports(workdir, [r["stderr"]])}
        runs.append(r)

    # 3. Any ThreadSanitizer warnings? (preprocessed: the model never sees raw reports)
    warned = [r for r in runs if "WARNING: ThreadSanitizer" in r["stderr"]]
    if warned:
        return {"status": "warnings",
                "runs_with_warnings": f"{len(warned)}/{NUM_RUNS}",
                **summarize_reports(workdir, [r["stderr"] for r in warned])}

    # 4. No TSan warnings, but a run crashed or exited non-zero: never report that as clean
    failed = [(i + 1, r) for i, r in enumerate(runs) if r["exit_code"] != 0]
    if failed:
        run_no, r = failed[0]
        result = {"status": "crashed", "run": run_no,
                  "runs_failed": f"{len(failed)}/{NUM_RUNS}",
                  "exit_code": r["exit_code"],
                  "message": "no ThreadSanitizer warnings, but the program crashed or exited "
                             "with a non-zero code; this is not a success"}
        if r["exit_code"] > 128:      # killed by a signal, e.g. 139 = SIGSEGV
            try:
                result["signal"] = signal.Signals(r["exit_code"] - 128).name
            except ValueError:
                pass
        lines = r["stderr"].splitlines()
        result["error_lines"] = [l.strip() for l in lines if "ERROR" in l or "error" in l.lower()][:3] \
            or [l.strip() for l in lines[-5:]]
        result["project_frames"] = _project_frames(workdir, lines)
        result["output_tail"] = r["stdout"][-500:]
        return result

    # 5. Clean in every run
    outputs = {r["stdout"] for r in runs}
    return {"status": "clean", "runs": NUM_RUNS,
            "consistent_output": len(outputs) == 1,
            "output": runs[0]["stdout"]}


# --- code tools: read_file, search_code, edit_file ---
READ_WHOLE_MAX_LINES = 300  # files up to this size are returned whole (later: config)
DEFAULT_WINDOW = 20         # lines above and below `line` for large files
MAX_MATCHES = 20            # search_code results sent to the model
SOURCE_EXTS = {".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hh", ".hxx"}


def _safe_path(workdir, file):
    """Resolve `file` inside `workdir`. Returns (path, None) or (None, error dict)."""
    root = Path(workdir).resolve()
    path = (root / file).resolve()
    if not path.is_relative_to(root):
        return None, {"error": f"'{file}' is outside the project folder"}
    if ".build" in path.relative_to(root).parts:
        return None, {"error": "build outputs cannot be read or edited"}
    if not path.is_file():
        return None, {"error": f"file not found: {file}"}
    return path, None


def _numbered(lines, start):
    """Lines with 1-based line numbers, e.g. '   9 | counter++;'."""
    return "\n".join(f"{n:>4} | {text}" for n, text in enumerate(lines, start))


def read_file(workdir, file, line=None, window=None):
    """Small files: the whole file. Large files: a window around `line`."""
    path, err = _safe_path(workdir, file)
    if err:
        return err
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError) as e:
        return {"error": f"cannot read {file}: {e}"}
    total = len(lines)
    if line is not None and not 1 <= line <= max(total, 1):
        return {"error": f"line {line} out of range (file has {total} lines)"}

    if total <= READ_WHOLE_MAX_LINES:
        return {"file": file, "total_lines": total, "content": _numbered(lines, 1)}

    window = window or DEFAULT_WINDOW
    center = line or 1
    start, end = max(1, center - window), min(total, center + window)
    return {"file": file, "total_lines": total, "showing": f"{start}-{end}",
            "content": _numbered(lines[start - 1:end], start),
            "note": "large file: only a window is shown. Use search_code to find related places "
                    "(other uses of the variable, other locks, thread creation)."}


def search_code(workdir, keyword, window=2):
    """Find `keyword` (plain text) in every source file in the project, with surrounding lines."""
    if not keyword:
        return {"error": "keyword is empty"}
    root = Path(workdir).resolve()
    matches = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.suffix not in SOURCE_EXTS or ".build" in rel.parts or not path.is_file():
            continue
        try:
            lines = path.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for i, text in enumerate(lines):
            if keyword in text:
                start, end = max(0, i - window), min(len(lines), i + window + 1)
                matches.append({"file": str(rel), "line": i + 1,
                                "context": _numbered(lines[start:end], start + 1)})
    result = {"keyword": keyword, "matches": matches[:MAX_MATCHES], "total_matches": len(matches)}
    if len(matches) > MAX_MATCHES:
        result["note"] = f"showing first {MAX_MATCHES} of {len(matches)}; use a more specific keyword"
    return result


def edit_file(workdir, file, old_text, new_text):
    """Replace exactly one occurrence of `old_text` with `new_text`. Returns a diff."""
    path, err = _safe_path(workdir, file)
    if err:
        return err
    if path.suffix not in SOURCE_EXTS:
        return {"error": f"only source files can be edited, not '{file}'"}
    if not old_text:
        return {"error": "old_text is empty"}
    if old_text == new_text:
        return {"error": "old_text and new_text are identical"}
    try:
        before = path.read_text()
    except (OSError, UnicodeDecodeError) as e:
        return {"error": f"cannot read {file}: {e}"}

    count = before.count(old_text)
    if count == 0:
        return {"error": "old_text not found. Copy it exactly from read_file output "
                         "(without the line-number prefix); whitespace must match."}
    if count > 1:
        return {"error": f"old_text found {count} times; include more surrounding lines "
                         "so it matches exactly one place."}

    after = before.replace(old_text, new_text, 1)
    path.write_text(after)
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(),
                                fromfile=f"a/{file}", tofile=f"b/{file}", lineterm="", n=2)
    return {"status": "edited", "file": file, "diff": "\n".join(diff)}


if __name__ == "__main__":
    import sys
    file = sys.argv[1] if len(sys.argv) > 1 else "race.cpp"
    workdir = sys.argv[2] if len(sys.argv) > 2 else "targets"
    print(json.dumps(run_tsan(workdir, file), indent=2))