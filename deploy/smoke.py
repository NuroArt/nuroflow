#!/usr/bin/env python3
"""End-to-end smoke test against a running NuroFlow stack, through the public edge.

    BASE_URL=http://localhost:8080 SMOKE_EMAIL=admin@example.com SMOKE_PASSWORD=... \\
        python3 deploy/smoke.py

Standard library only, so it runs on a bare server or in CI without installing anything.
Exits non-zero on the first failed check. It verifies the things that only break in a
real deployment: the edge proxy, security headers, SSE passing through unbuffered, the
worker and scheduler actually executing, and internal endpoints not leaking.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE = os.environ.get("BASE_URL", "http://localhost:8080").rstrip("/")
API = f"{BASE}/api/v1"
EMAIL = os.environ.get("SMOKE_EMAIL", "smoke@nuroworks.com")
PASSWORD = os.environ.get("SMOKE_PASSWORD", "")
results: list[tuple[str, bool, str]] = []


def request(
    method: str, url: str, body: Any = None, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as response:  # noqa: S310
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")
    if not ok:
        summary()
        sys.exit(1)


def summary() -> None:
    passed = sum(ok for _, ok, _ in results)
    print(f"\n{passed}/{len(results)} checks passed")


def main() -> None:
    print(f"Smoke testing {BASE}")
    deadline = time.monotonic() + 120
    status = 0
    while time.monotonic() < deadline:
        try:
            status, _, _ = request("GET", f"{BASE}/readyz")
            if status == 200:
                break
        except OSError:
            pass
        time.sleep(2)
    check("edge and API ready (/readyz)", status == 200)

    status, headers, body = request("GET", f"{BASE}/")
    csp = headers.get("Content-Security-Policy", "")
    check("dashboard served", status == 200 and b'<div id="root">' in body)
    check("strict CSP on the dashboard", "script-src 'self'" in csp and "unsafe" not in csp, csp[:60])
    check("SPA fallback for client routes", request("GET", f"{BASE}/runs/abc")[0] == 200)
    check("/metrics not exposed publicly", request("GET", f"{BASE}/metrics")[0] == 404)
    status, headers, _ = request("GET", f"{API}/no-such-endpoint")
    check(
        "unknown API paths return problem+json",
        status == 404 and headers.get("Content-Type") == "application/problem+json",
    )
    status, headers, _ = request("GET", f"{API}/auth/me")
    check("API rejects anonymous calls", status == 401)
    check("API security headers", headers.get("X-Content-Type-Options") == "nosniff")
    check("server version hidden", "nginx/" not in headers.get("Server", ""), headers.get("Server", ""))

    status, _, body = request("POST", f"{API}/auth/login", {"email": EMAIL, "password": PASSWORD})
    check("admin can log in", status == 200, f"HTTP {status}")
    auth = {"Authorization": f"Bearer {json.loads(body)['access_token']}"}
    status, _, _ = request("POST", f"{API}/auth/register", {"email": "x@y.io", "password": "x" * 16})
    check("public registration disabled", status in (403, 404), f"HTTP {status}")

    slug = f"smoke-{uuid.uuid4().hex[:8]}"
    definition = {
        "slug": slug,
        "name": "Smoke test",
        "tasks": [
            {"key": "wait", "operator": "control.delay", "inputs": {"seconds": 2}},
            {"key": "shape", "operator": "transform.template", "depends_on": ["wait"],
             "inputs": {"value": {"hello": "{{ params.who }}"}}},
        ],
        "params": {"who": "production"},
    }  # fmt: skip
    status, _, body = request("POST", f"{API}/workflows", {"definition": definition}, auth)
    check("workflow created", status == 201, f"HTTP {status}")
    workflow_id = json.loads(body)["id"]
    status, _, body = request("POST", f"{API}/workflows/{workflow_id}/runs", {}, auth)
    check("run accepted", status == 202, f"HTTP {status}")
    run_id = json.loads(body)["id"]

    # SSE must arrive *through nginx* while the run is in progress (proxy buffering off).
    started = time.monotonic()
    req = urllib.request.Request(f"{API}/runs/{run_id}/stream", headers=auth)
    first_event_after = None
    with urllib.request.urlopen(req, timeout=30) as stream:  # noqa: S310
        for raw in stream:
            if raw.startswith(b"event: snapshot") and first_event_after is None:
                first_event_after = time.monotonic() - started
            if raw.startswith(b"event: end"):
                break
    check(
        "SSE streams through the edge unbuffered",
        first_event_after is not None and first_event_after < 1.5,
        f"first snapshot after {first_event_after:.2f}s" if first_event_after else "no snapshot",
    )

    status, _, body = request("GET", f"{API}/runs/{run_id}", headers=auth)
    run = json.loads(body)
    check("worker executed the run", run["status"] == "succeeded", run["status"])
    shape = next(t for t in run["tasks"] if t["task_key"] == "shape")
    check("task output persisted", shape["output"] == {"value": {"hello": "production"}})

    status, _, body = request("GET", f"{API}/task-runs/{run['tasks'][0]['id']}/logs", headers=auth)
    check("operator logs recorded", status == 200 and len(json.loads(body)["items"]) >= 1)
    status, _, body = request("GET", f"{API}/stats/overview", headers=auth)
    check("overview stats", status == 200 and json.loads(body)["runs"] >= 1)

    request("DELETE", f"{API}/workflows/{workflow_id}", headers=auth)  # tidy up
    summary()


if __name__ == "__main__":
    main()
