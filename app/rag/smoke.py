"""Check the running RAG API without exposing credentials or requiring public ports."""

import argparse
import sys
import time

import httpx

from app.config import Settings


def check(client, require_index=False, require_complete=False):
    health = client.get("/health")
    health.raise_for_status()
    if health.json().get("status") != "ok":
        raise ValueError("RAG API is unhealthy")
    unauthorized = client.post(
        "/answer",
        json={"question": "Проверка доступа"},
        headers={"Authorization": "Bearer invalid-smoke-token"},
    )
    if unauthorized.status_code != 401:
        raise ValueError("RAG API did not reject invalid credentials")
    response = client.get("/index")
    response.raise_for_status()
    coverage = response.json()["coverage"]
    if require_index or require_complete:
        if not health.json().get("index_ready") or coverage.get("indexed_files", 0) < 1:
            raise ValueError("Waiting for the first searchable index")
    if require_complete and not coverage.get("complete"):
        raise ValueError("Index is incomplete; inspect indexing errors and saved checkpoints")
    return coverage


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-index", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--question", help="Also test a real answer (may call the LLM provider)")
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    settings = Settings()
    settings.require("rag_api_token")
    deadline = time.monotonic() + args.timeout
    with httpx.Client(
        base_url="http://127.0.0.1:8000",
        headers={"Authorization": "Bearer " + settings.rag_api_token.get_secret_value()},
        timeout=10,
    ) as client:
        while True:
            try:
                coverage = check(client, args.require_index, args.require_complete)
                break
            except (httpx.HTTPError, ValueError) as error:
                if time.monotonic() >= deadline:
                    # HTTP exceptions may include URLs: print only their type, never credentials.
                    print(f"Smoke check failed: {type(error).__name__}", file=sys.stderr)
                    return 1
                time.sleep(min(3, max(0, deadline - time.monotonic())))
        print(
            "RAG health/auth OK; "
            f"searchable files={coverage.get('indexed_files', 0)}/{coverage.get('total_files', 0)}; "
            f"complete={coverage.get('complete', False)}",
            flush=True,
        )
        if args.question:
            response = client.post(
                "/answer", json={"question": args.question}, timeout=settings.request_timeout
            )
            if response.status_code != 200:
                print(f"Answer check failed: HTTP {response.status_code}", file=sys.stderr)
                return 1
            answer = response.json()
            if not answer.get("answer", "").strip() or answer.get("route") not in {"faq", "rag"}:
                print("Answer check failed: no working FAQ/RAG answer", file=sys.stderr)
                return 1
            print(f"Answer OK; route={answer['route']}; tokens={answer.get('tokens', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
