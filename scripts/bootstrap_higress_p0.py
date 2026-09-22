"""Apply the versioned P0 Higress resources to the isolated all-in-one instance.

The all-in-one image persists a Kubernetes-like control plane below /data; that
state is intentionally ignored by Git. This script is the reproducible source
of truth and never prints credentials.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import ssl
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = ROOT / ".env.p0-test"
RESOURCE_FILE = ROOT / "config" / "higress" / "p0-test" / "resources.json"
IP_RESTRICTION_FILE = ROOT / "config" / "higress" / "p0-test" / "ip-restriction.json"
API_BASE = "https://127.0.0.1:18443"
RESOURCE_PATHS = {
    ("networking.higress.io/v1", "McpBridge"): "/apis/networking.higress.io/v1/namespaces/higress-system/mcpbridges",
    ("networking.k8s.io/v1", "Ingress"): "/apis/networking.k8s.io/v1/namespaces/higress-system/ingresses",
    ("extensions.higress.io/v1alpha1", "WasmPlugin"): "/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins",
}
TOKEN_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def load_env(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise SystemExit(f"Missing {path}. Run scripts/Initialize-P0TestEnvironment.ps1 first.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise SystemExit(f"Invalid dotenv entry in {path}: {line!r}")
        values[key] = value
    return values


def substitute(value: object, variables: dict[str, str]) -> object:
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if not isinstance(value, str):
        return value

    def replacement(match: re.Match[str]) -> str:
        key = match.group(1)
        secret = variables.get(key, "")
        if not secret:
            raise SystemExit(f"{key} is required in {DEFAULT_ENV.name}.")
        return secret

    return TOKEN_PATTERN.sub(replacement, value)


def api_request(opener, method: str, url: str, payload: object | None = None) -> tuple[int, object | None]:
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with opener.open(request, timeout=10) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else None
    except HTTPError as error:
        raw = error.read()
        try:
            body = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            body = {"raw": raw.decode("utf-8", errors="replace")[:500]}
        return error.code, body


def wait_for_api(opener) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            status, _ = api_request(opener, "GET", API_BASE + "/version")
            if status == 200:
                return
        except URLError:
            pass
        time.sleep(2)
    raise SystemExit("Timed out waiting for the P0 Higress control plane on 127.0.0.1:18443.")


def configure_legacy_key(resources: list[dict], legacy_key: str) -> None:
    key_auth = next(item for item in resources if item["metadata"]["name"] == "p0-key-auth")
    consumers = key_auth["spec"]["defaultConfig"]["consumers"]
    allow = key_auth["spec"]["matchRules"][0]["config"]["allow"]
    if legacy_key:
        consumers.append({
            "name": "p0-legacy", "keys": ["Authorization"],
            "credentials": ["Bearer " + legacy_key], "in_header": True, "in_query": False,
        })
    else:
        allow.remove("p0-legacy")


def override_tpm_limit(resources: list[dict], limit: int | None) -> None:
    """Apply a temporary test-only TPM override without editing tracked config."""
    if limit is None:
        return
    if limit <= 0:
        raise SystemExit("--tpm-limit must be a positive integer.")
    plugin = next((item for item in resources if item.get("metadata", {}).get("name") == "p0-ai-token-rate-limit"), None)
    if plugin is None:
        raise SystemExit("P0 token rate-limit plugin is missing from the resource file.")
    try:
        config = plugin["spec"]["matchRules"][0]["config"]
        config["rule_items"][0]["limit_keys"][0]["token_per_minute"] = limit
    except (KeyError, IndexError, TypeError) as error:
        raise SystemExit("P0 token rate-limit resource has an unexpected structure.") from error


def apply_resource(opener, resource: dict) -> None:
    key = (resource["apiVersion"], resource["kind"])
    collection = RESOURCE_PATHS.get(key)
    if collection is None:
        raise SystemExit(f"Unsupported P0 resource: {key}")
    name = resource["metadata"]["name"]
    item_url = API_BASE + collection + "/" + name
    status, existing = api_request(opener, "GET", item_url)
    desired = copy.deepcopy(resource)
    if status == 404:
        status, body = api_request(opener, "POST", API_BASE + collection, desired)
    elif status == 200 and isinstance(existing, dict):
        desired["metadata"]["resourceVersion"] = existing.get("metadata", {}).get("resourceVersion")
        status, body = api_request(opener, "PUT", item_url, desired)
    else:
        body = existing
    if status not in (200, 201):
        raise SystemExit(f"Failed to apply {resource['kind']}/{name}: HTTP {status}: {json.dumps(body, ensure_ascii=False)[:800]}")
    print(f"Applied {resource['kind']}/{name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--resource-file", type=Path, default=RESOURCE_FILE)
    parser.add_argument("--include-ip-restriction", action="store_true",
                        help="Also apply ip-restriction.json (requires IP Restriction file)")
    parser.add_argument("--tpm-limit", type=int,
                        help="Temporary test-only override for P0 TPM validation; never edits the source JSON")
    args = parser.parse_args()
    variables = load_env(args.env_file)
    resources = substitute(json.loads(args.resource_file.read_text(encoding="utf-8")), variables)

    # Optionally load IP Restriction plugin
    if args.include_ip_restriction:
        ip_file = ROOT / "config" / "higress" / "p0-test" / "ip-restriction.json"
        if not ip_file.is_file():
            raise SystemExit(f"IP Restriction file not found: {ip_file}")
        ip_resources = substitute(json.loads(ip_file.read_text(encoding="utf-8")), variables)
        resources.extend(ip_resources)

    configure_legacy_key(resources, variables.get("P0_HIGRESS_LEGACY_CONSUMER_KEY", ""))
    override_tpm_limit(resources, args.tpm_limit)
    context = ssl._create_unverified_context()
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    wait_for_api(opener)
    for resource in resources:
        apply_resource(opener, resource)
    print("P0 Higress bootstrap complete. Credentials were not printed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
