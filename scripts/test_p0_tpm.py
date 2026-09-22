"""Behaviorally test the Higress TPM limiter with a temporary one-token cap."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

from test_p0_gateway import call, call_stream, load_env


ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.p0-test"
COMPOSE = ROOT / "deploy" / "docker-compose.p0-test.yml"
BASE_URL = "http://ai-gateway-test.local:18080"


def compose(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", "compose", "--env-file", str(ENV), "-f", str(COMPOSE), *args], cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=timeout)


def bootstrap(test_limit: int | None = None) -> None:
    args = ["exec", "-T", "higress", "python3", "-X", "utf8", "/p0-scripts/bootstrap_higress_p0.py",
            "--env-file", "/p0-env/.env.p0-test", "--resource-file", "/p0-config/resources.json"]
    if test_limit is not None:
        args += ["--tpm-limit", str(test_limit)]
    result = compose(*args)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:800])


def flush_rate_databases() -> None:
    # The P0 Redis is an isolated, dedicated service. DB 0/1 are used only by
    # the P0 RPM and TPM tests; model state lives in PostgreSQL, not Redis.
    for index in (0, 1):
        result = compose("exec", "-T", "redis", "redis-cli", "-n", str(index), "FLUSHDB")
        if result.returncode != 0 or "OK" not in result.stdout:
            raise RuntimeError(f"Could not reset the isolated P0 Redis test database {index}.")


def main() -> int:
    variables = load_env(ENV)
    key = variables.get("P0_HIGRESS_CONSUMER_KEY", "")
    if not key:
        print("FAIL: missing P0 Consumer Key", file=sys.stderr)
        return 1
    opener = build_opener(ProxyHandler({}))
    overridden = False
    try:
        flush_rate_databases()
        overridden = True
        bootstrap(test_limit=1)
        status, done, text, usage, _ = call_stream(opener, BASE_URL, None, key, {
            "model": "my-qwen3.6-27b",
            "messages": [{"role": "user", "content": "Reply with x only."}],
            # Qwen3.6 can spend a small output budget on reasoning before a
            # visible token is emitted. Keep the test prompt tiny, but allow
            # enough headroom to get a usage-bearing terminal SSE chunk.
            "max_tokens": 512,
            "temperature": 0,
            "stream": True,
            "stream_options": {"include_usage": True},
        }, 45)
        if status != 200 or not done or not text or not usage:
            raise RuntimeError(f"Could not prime the P0 token counter with a usage-bearing Qwen stream (HTTP {status}).")
        print("PASS [TPM] A short Qwen stream returned usage and populated the isolated P0 Redis token window.")

        status, body, _ = call(opener, BASE_URL, None, key, "/v1/models", None, 30)
        error = body.get("error", {}) if isinstance(body, dict) else {}
        error_code = str(error.get("code", ""))
        if status != 429 or error_code != "token_rate_limit_exceeded":
            raise RuntimeError(f"Expected Higress TPM rejection (429/token_rate_limit_exceeded), received HTTP {status} code={error_code or 'unknown'}.")
        print("PASS [TPM] Temporary 1-token/Consumer test threshold returns 429 before normal model completion.")
    except Exception as error:
        print("FAIL [TPM] " + str(error), file=sys.stderr)
        return_code = 1
    else:
        return_code = 0
    finally:
        if overridden:
            try:
                bootstrap()
                flush_rate_databases()
                status, _, _ = call(opener, BASE_URL, None, key, "/v1/models", None, 30)
                if status != 200:
                    raise RuntimeError(f"Normal P0 config restore did not return to service (HTTP {status}).")
                print("PASS [TPM] Restored configured 20000 TPM threshold and verified model-list access.")
            except Exception as error:
                print("FAIL [TPM] Could not restore the normal 20000 TPM P0 config: " + str(error), file=sys.stderr)
                return_code = 1
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
