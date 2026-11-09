"""Run every offline check script and report one summary.

    python tests/run_all.py            # all checks
    python tests/run_all.py scoring    # only scripts whose name contains "scoring"
    python tests/run_all.py -v         # print every script's output, not just failures

Works from any directory. Each check_*.py runs in its own process from backend/,
as CI always ran them. check_end_to_end.py needs a live server, so this script
starts one on port 8123 and stops it afterwards.

That server runs from a throwaway copy of backend/ without .env, data/, logs/ or
outputs/, and with every *_API_KEY variable removed from its environment. The
end-to-end check expects a fresh clone (no keys, exactly the logs it generates)
and it saves and deletes a key, so running it against your working copy would
both fail and touch your real keys and run history. No network calls are made
beyond 127.0.0.1.

Exits 0 only if every script passes.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
BACKEND_DIR = TESTS_DIR.parent
E2E_SCRIPT = "check_end_to_end.py"
E2E_HOST, E2E_PORT = "127.0.0.1", 8123  # fixed in check_end_to_end.py
SCRIPT_TIMEOUT = 600
SERVER_START_TIMEOUT = 60

# Left out of the server's copy of backend/: local secrets, generated data and caches.
COPY_IGNORE = shutil.ignore_patterns(
    ".env", ".env.*", "data", "logs", "outputs", "external",
    ".venv", "venv", "__pycache__", "*.pyc", ".pytest_cache",
)


def keyless_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.endswith("_API_KEY")}
    env["PYTHONUNBUFFERED"] = "1"
    return env


def run_script(script: Path, cwd: Path, env: dict[str, str]) -> tuple[bool, str, str]:
    """Return (passed, one-line detail, full output)."""
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=cwd, env=env, capture_output=True, text=True, timeout=SCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        out = (exc.stdout or b"").decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return False, f"timed out after {SCRIPT_TIMEOUT}s", out
    output = proc.stdout + proc.stderr
    lines = output.splitlines()
    passed = sum(line.startswith("PASS ") for line in lines)
    failed = sum(line.startswith("FAIL ") for line in lines)
    detail = f"{passed} passed, {failed} failed"
    if proc.returncode != 0 and failed == 0:
        detail += f", exit code {proc.returncode}"
    return proc.returncode == 0, detail, output


def port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex((host, port)) == 0


def wait_for_health(server: subprocess.Popen, url: str) -> str | None:
    """Return None when the server answers, otherwise the reason it did not."""
    deadline = time.monotonic() + SERVER_START_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            return f"server exited with code {server.returncode} during startup"
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return None
        except OSError:
            pass
        time.sleep(0.3)
    return f"server did not answer {url} within {SERVER_START_TIMEOUT}s"


def stop(server: subprocess.Popen) -> None:
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def run_end_to_end(script: Path) -> tuple[bool, str, str]:
    if port_in_use(E2E_HOST, E2E_PORT):
        return False, f"port {E2E_PORT} is already in use; stop whatever is running there", ""
    workdir = Path(tempfile.mkdtemp(prefix="ir-e2e-"))
    server_log = workdir / "server.log"
    server = None
    try:
        backend = workdir / "backend"
        shutil.copytree(BACKEND_DIR, backend, ignore=COPY_IGNORE)
        env = keyless_env()
        with server_log.open("w") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "incident_response.api:app",
                 "--host", E2E_HOST, "--port", str(E2E_PORT)],
                cwd=backend, env=env, stdout=log, stderr=subprocess.STDOUT,
            )
        problem = wait_for_health(server, f"http://{E2E_HOST}:{E2E_PORT}/api/health")
        if problem:
            return False, problem, server_log.read_text()
        passed, detail, output = run_script(backend / "tests" / script.name, backend, env)
        if not passed:
            output += "\n--- server log ---\n" + server_log.read_text()
        return passed, detail, output
    finally:
        if server is not None:
            stop(server)
        shutil.rmtree(workdir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("filters", nargs="*", help="run only scripts whose name contains one of these")
    parser.add_argument("-v", "--verbose", action="store_true", help="print output of passing scripts too")
    args = parser.parse_args()

    scripts = sorted(TESTS_DIR.glob("check_*.py"), key=lambda p: (p.name == E2E_SCRIPT, p.name))
    if args.filters:
        scripts = [s for s in scripts if any(f in s.name for f in args.filters)]
    if not scripts:
        print("no check scripts matched")
        return 1

    print(f"Python {sys.version.split()[0]} · {len(scripts)} check scripts\n")
    results = []
    for script in scripts:
        print(f"→ {script.name} ...", flush=True)
        start = time.monotonic()
        if script.name == E2E_SCRIPT:
            passed, detail, output = run_end_to_end(script)
        else:
            passed, detail, output = run_script(script, BACKEND_DIR, keyless_env())
        seconds = time.monotonic() - start
        results.append((script.name, passed, detail, seconds))
        if args.verbose or not passed:
            print("\n".join("    " + line for line in output.rstrip().splitlines()))

    width = max(len(name) for name, *_ in results)
    print("\nSummary")
    for name, passed, detail, seconds in results:
        print(f"  {'PASS' if passed else 'FAIL'}  {name:<{width}}  {seconds:6.1f}s  {detail}")
    failures = [name for name, passed, *_ in results if not passed]
    print(f"\n{len(results) - len(failures)}/{len(results)} scripts passed"
          + (f"; failed: {', '.join(failures)}" if failures else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
