"""Benchmark: run the agent on every target several times and report success rate and cost.

Usage:  .venv/bin/python bench.py                      # every target in manifest, 3 runs each
        .venv/bin/python bench.py --runs 5 --parallel 4
        .venv/bin/python bench.py --targets race.cpp multi_var.cpp --no-review
Writes bench/<timestamp>.json with every run's report and the summary.
"""
import argparse
import json
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import agent

TARGETS_DIR = Path("targets")


def bench_targets(names=None):
    manifest = json.loads((TARGETS_DIR / "manifest.json").read_text())
    all_targets = [n for n, info in manifest.items() if not n.startswith("_") and info.get("bench", True)]
    if names:
        unknown = set(names) - set(manifest)
        if unknown:
            raise SystemExit(f"not in manifest: {', '.join(sorted(unknown))}")
        return names
    return all_targets


def one_run(target, runs_dir):
    """One agent run. Never raises: a crash in the harness becomes an 'exception' outcome."""
    try:
        report = agent.run_agent(str(TARGETS_DIR / target), runs_dir=runs_dir, verbose=False)
        log = Path(report["run_folder"]) / "log.jsonl"
        events = [json.loads(line) for line in log.read_text().splitlines()]
        report["review_rejections"] = sum(1 for e in events if e["event"] == "review" and e.get("verdict") == "reject")
        report["verify_failures"] = sum(1 for e in events if e["event"] == "verify" and not e["ok"])
        return target, report
    except Exception as e:
        return target, {"outcome": "exception", "success": False, "error": f"{type(e).__name__}: {e}",
                        "traceback": traceback.format_exc(), "cost_usd": 0, "turns": 0,
                        "run_tsan_calls": 0, "rejected_edits": 0, "seconds": 0,
                        "review_rejections": 0, "verify_failures": 0}


def summarize(results, targets):
    rows = []
    for t in targets:
        rs = [r for name, r in results if name == t]
        n = len(rs) or 1
        rows.append({
            "target": t,
            "success": f"{sum(1 for r in rs if r.get('success'))}/{len(rs)}",
            "outcomes": dict(Counter(r["outcome"] for r in rs)),
            "avg_cost": round(sum(r["cost_usd"] for r in rs) / n, 4),
            "avg_turns": round(sum(r["turns"] for r in rs) / n, 1),
            "avg_tsan_calls": round(sum(r["run_tsan_calls"] for r in rs) / n, 1),
            "rejected_edits": sum(r["rejected_edits"] for r in rs),
            "verify_failures": sum(r["verify_failures"] for r in rs),
            "review_rejections": sum(r["review_rejections"] for r in rs),
            "avg_seconds": round(sum(r["seconds"] for r in rs) / n, 1),
            "needs_manual_review": sum(1 for r in rs if r.get("needs_manual_review")),
        })
    return rows


def print_table(rows, total_cost, wall, n_ok, n_all):
    cols = [("target", 16), ("success", 8), ("avg_cost", 9), ("avg_turns", 10), ("avg_tsan_calls", 15),
            ("rejected_edits", 15), ("verify_failures", 16), ("review_rejections", 18), ("avg_seconds", 12)]
    print("\n" + " ".join(name.ljust(w) for name, w in cols) + " outcomes")
    for r in rows:
        print(" ".join(str(r[name]).ljust(w) for name, w in cols) + " " + json.dumps(r["outcomes"]))
    print(f"\nsuccess {n_ok}/{n_all} ({100 * n_ok / max(n_all, 1):.0f}%), "
          f"total cost ${total_cost:.4f}, wall time {wall:.0f}s")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", type=int, default=3, help="runs per target (default 3)")
    p.add_argument("--parallel", type=int, default=3, help="runs at the same time (default 3)")
    p.add_argument("--targets", nargs="*", help="target file names from targets/manifest.json")
    p.add_argument("--no-review", action="store_true", help="skip the LLM review step")
    args = p.parse_args()

    if args.no_review:
        agent.REVIEW_ENABLED = False
    targets = bench_targets(args.targets)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    runs_dir = f"runs/bench-{stamp}"
    jobs = [t for t in targets for _ in range(args.runs)]
    print(f"bench {stamp}: {len(targets)} targets x {args.runs} runs = {len(jobs)} runs, "
          f"{args.parallel} at a time, model {agent.MODEL} ({agent.EFFORT}), "
          f"review {'on' if agent.REVIEW_ENABLED else 'off'}")

    started, results = time.time(), []
    with ThreadPoolExecutor(max_workers=args.parallel) as pool:
        futures = [pool.submit(one_run, t, runs_dir) for t in jobs]
        for f in as_completed(futures):
            target, report = f.result()
            results.append((target, report))
            mark = "ok  " if report.get("success") else "FAIL"
            print(f"  [{len(results):>2}/{len(jobs)}] {mark} {target:16} {report['outcome']:14} "
                  f"${report['cost_usd']:.4f} {report['turns']} turns"
                  + (f"  ({report.get('stop_reason') or report.get('error')})" if not report.get("success") else ""))

    wall = time.time() - started
    rows = summarize(results, targets)
    n_ok = sum(1 for _, r in results if r.get("success"))
    total_cost = sum(r["cost_usd"] for _, r in results)
    print_table(rows, total_cost, wall, n_ok, len(results))

    out = Path("bench") / f"{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "stamp": stamp, "model": agent.MODEL, "effort": agent.EFFORT, "review": agent.REVIEW_ENABLED,
        "runs_per_target": args.runs, "total_cost_usd": round(total_cost, 4), "wall_seconds": round(wall, 1),
        "success": f"{n_ok}/{len(results)}", "summary": rows,
        "runs": [{"target": t, **r} for t, r in results],
    }, indent=2, default=str))
    print(f"saved {out} (run folders in {runs_dir}/)")


if __name__ == "__main__":
    main()
