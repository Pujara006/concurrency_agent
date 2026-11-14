import json
import subprocess
from pathlib import Path

BUILD_FLAGS = "-std=c++17 -fsanitize=thread -g -O1"   # later: move to config


def run_in_docker(workdir, command, timeout=60):
    """Run a shell command inside a Linux container with `workdir` mounted at /work."""
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{Path(workdir).resolve()}:/work",
        "-w", "/work",
        "gcc:14",
        "bash", "-c", command,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def run_tsan(workdir, file):
    """Build `file` with ThreadSanitizer, run it once, and report the outcome."""
    # 1. Build. The binary goes into .build/ inside the shared folder,
    #    because each --rm container is thrown away when it finishes
    build = run_in_docker(workdir, f"mkdir -p .build && g++ {BUILD_FLAGS} {file} -o .build/prog")
    if build["exit_code"] != 0:
        return {"status": "build_failed", "errors": build["stderr"][-2000:]}

    # 2. Run
    run = run_in_docker(workdir, "./.build/prog")
    if "WARNING: ThreadSanitizer" in run["stderr"]:
        return {"status": "warnings", "report": run["stderr"], "output": run["stdout"]}
    return {"status": "clean", "output": run["stdout"]}


if __name__ == "__main__":
    print(json.dumps(run_tsan("targets", "race.cpp"), indent=2)[:800])