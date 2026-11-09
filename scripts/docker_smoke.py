#!/usr/bin/env python3
"""Smoke-test the Docker stack end to end, with no API keys.

Builds and starts a throwaway copy of docker-compose.yml under its own project
name, on its own ports, with empty volumes in place of backend/data, logs and
outputs. A dev stack and its data are left alone, and every run starts clean.

Then it checks:
  * the dashboard and its assets are served by the frontend container
  * only the offline mock provider is available (no keys reached the stack)
  * the backend passes backend/tests/check_end_to_end.py
  * through the frontend's /api proxy, as a browser would: a log is
    generated, a mock diagnosis streams to a result and a mock sweep
    streams to completion

The stack is removed afterwards unless --keep is given. Needs only Docker
with Compose v2 and Python 3.8+.

    python3 scripts/docker_smoke.py [--keep] [--web-port N] [--api-port N]
"""

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECT = "avp-smoke"
KEY_VARS = ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "GROQ_MODELS")

# Empty volumes over the bind mounts, matched by target path.
OVERRIDE = """services:
  backend:
    volumes:
      - /app/data
      - /app/logs
      - /app/outputs
"""

results = []


def check(name, ok, detail=""):
    results.append((name, "PASS" if ok else "FAIL"))
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail != "" else ""), flush=True)
    return ok


def http(method, url, body=None, timeout=60):
    """Return (status, content type, text); HTTP errors are results, not exceptions."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.headers.get("Content-Type", ""), response.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Content-Type", ""), exc.read().decode()
    except (urllib.error.URLError, OSError) as exc:
        return 0, "", str(exc)


def sse_events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def port_free(port):
    # Probe for a listener rather than binding: a bind also fails on the
    # TIME_WAIT sockets a previous run leaves behind, which Docker ignores.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def wait_for_health(url, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, _ = http("GET", url, timeout=5)
        if status == 200:
            return True
        time.sleep(1)
    return False


def check_dashboard(web):
    status, ctype, html = http("GET", web + "/")
    check("dashboard HTML is served", status == 200 and "text/html" in ctype and 'id="root"' in html, status)

    assets = sorted(set(re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)))
    has_js = any(path.endswith(".js") for path in assets)
    has_css = any(path.endswith(".css") for path in assets)
    sizes = {}
    for path in assets:
        asset_status, _, body = http("GET", web + path)
        sizes[path] = len(body) if asset_status == 200 else asset_status
    check(
        "dashboard JS and CSS bundles are served",
        has_js and has_css and all(isinstance(size, int) and size > 1000 for size in sizes.values()),
        ", ".join(f"{path.rsplit('/', 1)[-1]}={size}" for path, size in sizes.items()) or "no assets",
    )

    status, _, html = http("GET", web + "/runs")
    check("client-side routes fall back to the app", status == 200 and 'id="root"' in html, status)


def check_no_keys(web):
    status, _, text = http("GET", web + "/api/providers")
    try:
        state = {p["provider"]: p["available"] for p in json.loads(text)["providers"]}
    except (ValueError, KeyError, TypeError):
        state = text[:120]
    check(
        "no API keys: only the mock provider is available",
        status == 200 and state == {"anthropic": False, "groq": False, "mock": True},
        state,
    )


def check_backend_suite(api):
    print("---- backend/tests/check_end_to_end.py against the backend container", flush=True)
    env = dict(os.environ, E2E_BASE_URL=api)
    completed = subprocess.run(
        [sys.executable, str(REPO / "backend" / "tests" / "check_end_to_end.py")],
        env=env, capture_output=True, text=True, timeout=600,
    )
    for line in (completed.stdout + completed.stderr).strip().splitlines():
        print("     " + line)
    check("backend end-to-end suite passes in the container", completed.returncode == 0, f"exit {completed.returncode}")


def check_browser_flows(web):
    """The calls the dashboard makes, with its payloads, through the nginx proxy."""
    print("---- dashboard flows through the frontend's /api proxy", flush=True)
    status, _, text = http("POST", web + "/api/logs/generate",
                           {"fault_type": "pool_exhausted", "run_id": 901, "requests": 30, "noise": True})
    generated = json.loads(text).get("generated", []) if status == 200 else []
    name = generated[0]["name"] if generated else None
    check("generate a log", status == 200 and len(generated) == 1, name or text[:120])
    listed = [log["name"] for log in json.loads(http("GET", web + "/api/logs")[2]).get("logs", [])]
    check("the generated log is listed", name in listed, f"{len(listed)} logs")
    if not name:
        return

    for condition in ("single_agent", "pipeline"):
        status, ctype, text = http("POST", web + "/api/diagnose/stream",
                                   {"log_name": name, "condition": condition, "provider": "mock",
                                    "model": None, "recovery": False})
        events = sse_events(text) if status == 200 else []
        types = [event.get("type") for event in events]
        result = events[-1] if events and types[-1] == "result" else None
        check(
            f"mock diagnosis ({condition}) streams to a result",
            "text/event-stream" in ctype and types[:1] == ["run_start"] and "error" not in types
            and result is not None and result["result"]["predicted_fault"] == "pool_exhausted"
            and isinstance(result.get("run_id"), int),
            f"{len(events)} events, predicted={result['result']['predicted_fault'] if result else None}",
        )

    status, _, text = http("POST", web + "/api/diagnose/stream",
                           {"log_name": name, "condition": "pipeline", "provider": "groq"})
    check("a provider without a key fails with a clear error", status == 400 and "GROQ_API_KEY" in text, status)

    status, _, text = http("POST", web + "/api/sweep",
                           {"arms": ["mock:"], "conditions": ["single_agent", "pipeline"], "hint": True,
                            "log_names": [name], "recovery": False})
    sweep = json.loads(text) if status == 200 else {}
    check("mock sweep starts", status == 200 and sweep.get("total") == 2, sweep.get("total", text[:120]))
    if not sweep:
        return

    status, ctype, text = http("GET", web + f"/api/sweep/{sweep['sweep_id']}/stream", timeout=120)
    events = sse_events(text) if status == 200 else []
    last = events[-1] if events else {}
    failures = [event for event in events if event.get("type") in ("error", "arm_failed")]
    check(
        "mock sweep streams to completion",
        "text/event-stream" in ctype and last.get("type") == "sweep_done"
        and last.get("completed") == sweep["total"] and not failures,
        f"{last.get('type')} {last.get('completed')}/{sweep['total']}",
    )

    stats = json.loads(http("GET", web + f"/api/stats?sweep_id={sweep['sweep_id']}")[2])
    runs = json.loads(http("GET", web + f"/api/runs?sweep_id={sweep['sweep_id']}")[2])["runs"]
    check(
        "sweep results are recorded",
        sorted(row["condition"] for row in stats.get("by_arm", [])) == ["pipeline", "single_agent"]
        and len(runs) == 2,
        f"{len(stats.get('by_arm', []))} arm rows, {len(runs)} runs",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep", action="store_true", help="leave the stack running afterwards")
    parser.add_argument("--web-port", type=int, default=18080)
    parser.add_argument("--api-port", type=int, default=18000)
    parser.add_argument("--timeout", type=int, default=120, help="seconds to wait for the stack to answer")
    args = parser.parse_args()

    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        print("Docker is not running (docker info failed). Start Docker Desktop and retry.")
        return 2
    version = subprocess.run(["docker", "compose", "version", "--short"], capture_output=True, text=True)
    if version.returncode != 0:
        print("`docker compose` (Compose v2) is not available.")
        return 2
    busy = [port for port in (args.web_port, args.api_port) if not port_free(port)]
    if busy:
        print(f"Port(s) {busy} are in use. Pick others with --web-port / --api-port.")
        return 2
    print(f"Docker Compose {version.stdout.strip()}; project {PROJECT}; "
          f"web 127.0.0.1:{args.web_port}, api 127.0.0.1:{args.api_port}", flush=True)

    workdir = Path(tempfile.mkdtemp(prefix="avp-smoke-"))
    (workdir / "override.yml").write_text(OVERRIDE)
    (workdir / "empty.env").write_text("")  # so no .env file can supply a key
    env = {key: value for key, value in os.environ.items() if key not in KEY_VARS}
    env.update(AVP_WEB_PORT=str(args.web_port), AVP_API_PORT=str(args.api_port))
    compose = ["docker", "compose", "-p", PROJECT, "--env-file", str(workdir / "empty.env"),
               "-f", str(REPO / "docker-compose.yml"), "-f", str(workdir / "override.yml")]
    web = f"http://127.0.0.1:{args.web_port}"
    api = f"http://127.0.0.1:{args.api_port}"

    started = False
    try:
        print("---- docker compose up --build", flush=True)
        up = subprocess.run(compose + ["up", "--build", "--detach"], cwd=REPO, env=env)
        started = True
        if check("docker compose up --build", up.returncode == 0, f"exit {up.returncode}"):
            if check("stack answers through the frontend", wait_for_health(web + "/api/health", args.timeout)):
                check_dashboard(web)
                check_no_keys(web)
                check_backend_suite(api)
                check_browser_flows(web)
    except KeyboardInterrupt:
        check("interrupted", False)
    finally:
        failed = [name for name, outcome in results if outcome == "FAIL"]
        if failed and started:
            print("---- backend and frontend logs (last 60 lines each)", flush=True)
            subprocess.run(compose + ["logs", "--no-color", "--tail", "60"], cwd=REPO, env=env)
        if started and not args.keep:
            subprocess.run(compose + ["down", "--volumes", "--remove-orphans"], cwd=REPO, env=env,
                           capture_output=True)
        elif started:
            print(f"\nStack left running: {web}  (stop it with: docker compose -p {PROJECT} down -v)")
        for path in workdir.iterdir():
            path.unlink()
        workdir.rmdir()

    passed = sum(outcome == "PASS" for _, outcome in results)
    print(f"\n{passed} passed, {len(failed)} failed" + (": " + ", ".join(failed) if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
