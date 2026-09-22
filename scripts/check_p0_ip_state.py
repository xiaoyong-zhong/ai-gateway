"""Confirm the deferred P0 IP restriction has not been activated prematurely."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.p0-test"
COMPOSE = ROOT / "deploy" / "docker-compose.p0-test.yml"
RESOURCE = ROOT / "config" / "higress" / "p0-test" / "resources.json"
RUNTIME_URL = "https://127.0.0.1:18443/apis/extensions.higress.io/v1alpha1/namespaces/higress-system/wasmplugins/p0-ip-restriction"


def main() -> int:
    if not ENV.is_file():
        print("FAIL: missing isolated .env.p0-test", file=sys.stderr)
        return 1
    resources = json.loads(RESOURCE.read_text(encoding="utf-8"))
    declared = [item for item in resources if item.get("metadata", {}).get("name") == "p0-ip-restriction"]
    if declared:
        print("FAIL: p0-ip-restriction is present in the default apply manifest; remove it until Ops approves CIDR.")
        return 1

    python_probe = f'''import json,ssl,urllib.request,urllib.error
u = {RUNTIME_URL!r}
request = urllib.request.Request(u)
context = ssl._create_unverified_context()
try:
    response = urllib.request.urlopen(request, context=context, timeout=5)
    print(json.dumps({{"status": response.status, "body": json.loads(response.read())}}))
except urllib.error.HTTPError as error:
    print(json.dumps({{"status": error.code}}))
'''
    command = [
        "docker", "compose", "--env-file", str(ENV), "-f", str(COMPOSE),
        "exec", "-T", "higress", "python3", "-c", python_probe,
    ]
    result = subprocess.run(command, cwd=ROOT, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if result.returncode != 0:
        print("FAIL: could not inspect the P0 Higress runtime plugin state")
        print((result.stderr or result.stdout).strip()[:400])
        return 1
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError):
        print("FAIL: Higress runtime plugin probe did not return a parseable status")
        return 1
    status = data.get("status")
    if status == 404:
        print("SKIP: IP Restriction remains unapplied pending Ops-approved CIDR; runtime plugin is absent.")
        return 0
    if status == 200:
        resource = data.get("body", {})
        spec = resource.get("spec", {})
        rules = spec.get("matchRules", [])
        enabled = not spec.get("defaultConfigDisable", False) and any(not rule.get("configDisable", False) for rule in rules)
        if enabled:
            print("FAIL: p0-ip-restriction is active before CIDR approval.")
            return 1
        print("SKIP: IP Restriction resource exists but is disabled; awaiting Ops-approved CIDR.")
        return 0
    print(f"FAIL: unexpected Higress API status {status!r} while checking deferred IP policy.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
