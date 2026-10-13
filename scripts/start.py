"""Start the published system through GitHub Actions, using existing Git credentials."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def github_token():
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if token := os.environ.get(name, "").strip():
            return token
    try:
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=30,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"},
        )
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if result.returncode == 0 and values.get("password"):
            return values["password"]
    except (OSError, subprocess.TimeoutExpired):
        pass
    raise RuntimeError("GitHub authentication unavailable: configure Git or GH_TOKEN.")


class GitHub:
    def __init__(self, repo, token):
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
            raise ValueError("Repository must be OWNER/NAME")
        self.base = f"https://api.github.com/repos/{repo}"
        self.token = token

    def request(self, path, data=None):
        request = Request(
            self.base + path,
            data=json.dumps(data).encode() if data is not None else None,
            headers={
                "Authorization": "Bearer " + self.token,
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "miem-bot-start",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read()
                return json.loads(content) if content else None
        except HTTPError as error:
            raise RuntimeError(
                f"GitHub HTTP {error.code}: check repository access and Actions permissions."
            ) from None
        except URLError:
            raise RuntimeError("GitHub is unreachable; check the network connection.") from None


def find_run(client, request_id, ref):
    query = urlencode({"event": "workflow_dispatch", "branch": ref, "per_page": 100})
    runs = client.request(f"/actions/workflows/ci-cd.yml/runs?{query}")["workflow_runs"]
    return next((run for run in runs if run["display_title"].endswith(request_id)), None)


def wait_for_run(client, request_id, ref, timeout, wait=True):
    deadline = time.monotonic() + timeout
    run = None
    previous = None
    while time.monotonic() < deadline:
        run = (
            client.request(f"/actions/runs/{run['id']}")
            if run
            else find_run(client, request_id, ref)
        )
        if run:
            if previous is None:
                print(run["html_url"], flush=True)
            current = (run["status"], run.get("conclusion"))
            if current != previous:
                print(f"Workflow: {current[0]} ({current[1] or 'pending'})", flush=True)
                previous = current
            if not wait:
                return run
            if run["status"] == "completed":
                if run["conclusion"] != "success":
                    raise RuntimeError(
                        f"Workflow ended with {run['conclusion']}: {run['html_url']}"
                    )
                jobs = client.request(f"/actions/runs/{run['id']}/jobs?per_page=100")["jobs"]
                deploy = next((job for job in jobs if job["name"] == "deploy"), None)
                if deploy is None or deploy["conclusion"] != "success":
                    raise RuntimeError("CI passed, but deployment was skipped or did not succeed.")
                return run
        time.sleep(10)
    suffix = f": {run['html_url']}" if run else ""
    raise RuntimeError(f"Timed out waiting for Actions; the workflow may still be running{suffix}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("prepare", "deploy", "index"), default="index")
    parser.add_argument("--repo", default="Willi-Wonkaa/miem_bot")
    parser.add_argument("--ref", default="main", choices=("main",))
    parser.add_argument("--no-wait", action="store_true", help="Print the run URL and exit")
    parser.add_argument("--timeout", type=int, default=4 * 60 * 60, help="Wait limit in seconds")
    args = parser.parse_args(argv)
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    try:
        client = GitHub(args.repo, github_token())
        workflow = client.request("/actions/workflows/ci-cd.yml")
        if workflow["state"] != "active":
            raise RuntimeError("CI and deploy workflow is disabled.")
        request_id = uuid.uuid4().hex
        client.request(
            "/actions/workflows/ci-cd.yml/dispatches",
            {"ref": args.ref, "inputs": {"mode": args.mode, "request_id": request_id}},
        )
        print(
            f"Requested {args.mode} from {args.repo}@{args.ref}; request {request_id}", flush=True
        )
        wait_for_run(client, request_id, args.ref, args.timeout, wait=not args.no_wait)
        return 0
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Stopped waiting; the Actions workflow continues.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
