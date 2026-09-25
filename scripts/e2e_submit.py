#!/usr/bin/env python3
"""End-to-end pipeline check: register → verify → submit → verdict (DESIGN.md §5).

Registers a user, submits a wrong then a correct solution to the bundled
`design-vending-machine` starter problem, and asserts the verdicts. The code comes
from that problem's own seed file (its first editorial solution is correct; its
starter code, whose methods all return None, is wrong), so this can't drift from
what `app.cli seed` loaded. Requires a running API + arq worker + Docker, with the
DB migrated and seeded.

    BASE_URL=http://localhost:8000 python scripts/e2e_submit.py
"""
import asyncio
import json
import os
import pathlib
import secrets
import subprocess
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE = os.environ.get("BASE_URL", "http://localhost:8000")
API = f"{BASE}/api/v1"

PROBLEM = json.loads((ROOT / "seed" / "problems" / "design-vending-machine.json").read_text())
WRONG = PROBLEM["starter_code"]
CORRECT = PROBLEM["solutions"][0]["code"]


async def poll(client, headers, sid, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = (await client.get(f"{API}/submissions/{sid}", headers=headers)).json()
        if body["status"] not in ("pending", "running"):
            return body
        await asyncio.sleep(1)
    raise SystemExit(f"poll timed out for submission {sid}")


async def submit(client, headers, path, pid, code):
    r = await client.post(f"{API}{path}", headers=headers, json={"problem_id": pid, "code": code})
    r.raise_for_status()
    return await poll(client, headers, r.json()["id"])


async def main():
    async with httpx.AsyncClient(timeout=15) as c:
        email = f"e2e-{secrets.token_hex(4)}@example.com"
        username = "e2e_" + secrets.token_hex(3)
        assert (await c.post(f"{API}/auth/register", json={
            "email": email, "username": username, "password": "sunflower-desk-42"})).status_code == 202
        # The judge endpoints require a verified email (403 otherwise), and there's
        # no inbox here — verify this throwaway account out-of-band via the CLI.
        subprocess.run(
            [sys.executable, "-m", "app.cli", "verify-email", email],
            cwd=ROOT / "backend", check=True)
        token = (await c.post(f"{API}/auth/login", json={
            "email": email, "password": "sunflower-desk-42"})).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        pid = (await c.get(f"{API}/problems/{PROBLEM['slug']}", headers=headers)).json()["id"]

        wrong = await submit(c, headers, "/submissions", pid, WRONG)
        assert wrong["status"] == "wrong_answer", wrong
        print(f"wrong solution   -> {wrong['status']} ✓")

        correct = await submit(c, headers, "/submissions", pid, CORRECT)
        assert correct["status"] == "accepted", correct
        print(f"correct solution -> {correct['status']} ({correct['runtime_ms']} ms) ✓")

        run = await submit(c, headers, "/run", pid, CORRECT)
        assert run["status"] == "accepted", run
        print(f"run (samples)    -> {run['status']} ✓")

        print("E2E OK")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as exc:
        print(f"E2E FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
