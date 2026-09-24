"""Apply the isolated P1 Higress resources without printing credentials."""

from __future__ import annotations

import argparse
import copy
import json
import re
import ssl
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener


API_BASE = "https://127.0.0.1:18443"
RESOURCE_PATHS = {
    ("networking.higress.io/v1", "McpBridge"): "/apis/networking.higress.io/v1/namespaces/higress-system/mcpbridges",
    ("networking.k8s.io/v1", "Ingress"): "/apis/networking.k8s.io/v1/namespaces/higress-system/ingresses",
    ("extensions.higress.io/v1alpha1", "WasmPlugin"): "/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins",
}
TOKEN_PATTERN = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def substitute(value: object, variables: dict[str, str]) -> object:
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    if not isinstance(value, str):
        return value

    def replace(match: re.Match[str]) -> str:
        result = variables.get(match.group(1), "")
        if not result:
            raise SystemExit(f"Missing required P1 variable: {match.group(1)}")
        return result

    return TOKEN_PATTERN.sub(replace, value)


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
            body = None
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
    raise SystemExit("Timed out waiting for the P1 Higress control plane.")


def apply_resource(opener, resource: dict) -> None:
    kind = (resource["apiVersion"], resource["kind"])
    collection = RESOURCE_PATHS.get(kind)
    if collection is None:
        raise SystemExit(f"Unsupported P1 resource: {kind}")
    name = resource["metadata"]["name"]
    item_url = API_BASE + collection + "/" + name
    status, existing = api_request(opener, "GET", item_url)
    desired = copy.deepcopy(resource)
    if status == 404:
        status, _ = api_request(opener, "POST", API_BASE + collection, desired)
    elif status == 200 and isinstance(existing, dict):
        desired["metadata"]["resourceVersion"] = existing.get("metadata", {}).get("resourceVersion")
        status, _ = api_request(opener, "PUT", item_url, desired)
    if status not in (200, 201):
        raise SystemExit(f"Failed to apply {resource['kind']}/{name}: HTTP {status}")
    print(f"Applied {resource['kind']}/{name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--resource-file", type=Path, required=True)
    args = parser.parse_args()
    variables = load_env(args.env_file)
    # Windows PowerShell 5.1 may emit a UTF-8 BOM when a local transaction
    # updates the template. Accept both BOM and BOM-less JSON manifests.
    resources = substitute(json.loads(args.resource_file.read_text(encoding="utf-8-sig")), variables)
    opener = build_opener(ProxyHandler({}), HTTPSHandler(context=ssl._create_unverified_context()))
    wait_for_api(opener)
    for resource in resources:
        apply_resource(opener, resource)
    print("P1 Higress bootstrap complete. Credentials were not printed.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
