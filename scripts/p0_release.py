"""Local-only snapshots and rollback for the isolated P0 Docker stack."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "docker-compose.p0-test.yml"
ENV_FILE = ROOT / ".env.p0-test"
SNAPSHOT_ROOT = ROOT / "runtime" / "p0-test" / "releases"
MANAGED_FILES = (
    "deploy/docker-compose.p0-test.yml",
    "config/higress/p0-test/resources.json",
    "config/higress/p0-test/ip-restriction.json",
    "config/litellm.yaml",
    "gateway/litellm/Dockerfile",
    "gateway/litellm/callback/qwen_responses_compat.py",
    "gateway/litellm/test/test_qwen_compat.py",
    "gateway/litellm/test/test_streaming.py",
)
SECRET_FILES = (".env.p0-test",)


def run(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, input=input_text, text=True, encoding="utf-8", errors="replace", capture_output=True)
    if check and result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"Command failed ({result.returncode}): {args[0]} {args[1] if len(args) > 1 else ''}\n{details}")
    return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_commit() -> str:
    result = run(["git", "rev-parse", "HEAD"], check=False)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def compose(args: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    return run(["docker", "compose", "--env-file", str(ENV_FILE), "-f", str(COMPOSE), *args], **kwargs)


def make_snapshot(kind: str, message: str) -> str:
    if not ENV_FILE.is_file():
        raise RuntimeError("Missing .env.p0-test; initialize the isolated P0 environment first.")
    release_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    target = SNAPSHOT_ROOT / release_id
    target.mkdir(parents=True, exist_ok=False)
    copied: list[dict[str, str]] = []
    for rel in MANAGED_FILES + SECRET_FILES:
        source = ROOT / rel
        if not source.is_file():
            if rel in SECRET_FILES:
                raise RuntimeError(f"Required local secret file is missing: {rel}")
            continue
        destination = target / "files" / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        entry = {"path": rel}
        if rel not in SECRET_FILES:
            entry["sha256"] = sha256(source)
        copied.append(entry)

    images: list[str] = []
    image_result = compose(["images", "--format", "json"], check=False)
    if image_result.returncode == 0:
        try:
            for item in json.loads(image_result.stdout or "[]"):
                image_id = item.get("ID") or item.get("ImageID")
                if image_id:
                    images.append(str(image_id))
        except json.JSONDecodeError:
            images = []
    manifest = {
        "schema_version": 1,
        "release_id": release_id,
        "kind": kind,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "message": message[:160],
        "git_commit": git_commit(),
        "files": copied,
        "local_secret_snapshot": True,
        "image_ids": images,
    }
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return release_id


def load_manifest(release_id: str) -> tuple[Path, dict]:
    target = SNAPSHOT_ROOT / release_id
    manifest_path = target / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError(f"Snapshot not found: {release_id}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or manifest.get("release_id") != release_id:
        raise RuntimeError(f"Snapshot manifest is invalid: {release_id}")
    return target, manifest


def restore_files(target: Path, manifest: dict) -> None:
    listed = {entry["path"] for entry in manifest.get("files", [])}
    required = set(MANAGED_FILES) | set(SECRET_FILES)
    if not required.issubset(listed):
        missing = sorted(required - listed)
        raise RuntimeError("Snapshot lacks required files: " + ", ".join(missing))
    for entry in manifest["files"]:
        rel = entry["path"]
        if rel not in required:
            raise RuntimeError(f"Snapshot contains an unmanaged path: {rel}")
        source = target / "files" / rel
        destination = ROOT / rel
        if not source.is_file():
            raise RuntimeError(f"Snapshot file is missing: {rel}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".p0-restore-tmp")
        shutil.copy2(source, temporary)
        temporary.replace(destination)
        expected = entry.get("sha256")
        if expected and sha256(destination) != expected:
            raise RuntimeError(f"Restored file hash mismatch: {rel}")


def apply_local_stack() -> None:
    compose(["config", "-q"])
    compose(["up", "-d", "--build"])
    compose([
        "exec", "-T", "higress", "python3", "-X", "utf8",
        "/p0-scripts/bootstrap_higress_p0.py",
        "--env-file", "/p0-env/.env.p0-test",
        "--resource-file", "/p0-config/resources.json",
    ])


def command_backup(message: str) -> int:
    release_id = make_snapshot("release", message)
    print(f"Created local P0 snapshot: {release_id}")
    print("Snapshot files are under runtime/p0-test/releases (Git-ignored); local secrets are not listed or printed.")
    return 0


def command_list() -> int:
    if not SNAPSHOT_ROOT.is_dir():
        print("No local P0 snapshots found.")
        return 0
    rows = []
    for path in SNAPSHOT_ROOT.iterdir():
        if not path.is_dir() or not (path / "manifest.json").is_file():
            continue
        try:
            manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows.append(manifest)
    for item in sorted(rows, key=lambda row: row.get("created_at", ""), reverse=True):
        print(f"{item.get('release_id')}  {item.get('kind')}  {item.get('created_at')}  {item.get('message', '')}")
    return 0


def command_rollback(release_id: str) -> int:
    target, manifest = load_manifest(release_id)
    if manifest.get("kind") != "release":
        raise RuntimeError("Only a successful-release snapshot can be selected for rollback.")
    safety_id = make_snapshot("safety", "automatic pre-rollback recovery point")
    safety_path, safety_manifest = load_manifest(safety_id)
    print(f"Created automatic recovery point: {safety_id}")
    try:
        restore_files(target, manifest)
        apply_local_stack()
        print(f"Rollback applied locally: {release_id}")
        return 0
    except Exception as error:
        print(f"Rollback failed; restoring pre-rollback snapshot {safety_id}.", file=sys.stderr)
        try:
            restore_files(safety_path, safety_manifest)
            apply_local_stack()
        except Exception as recovery_error:
            raise RuntimeError(f"Rollback and automatic recovery both failed: {error}; {recovery_error}") from recovery_error
        raise RuntimeError(f"Rollback failed; prior local configuration was restored: {error}") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("backup")
    backup.add_argument("--message", default="manual local P0 snapshot")
    sub.add_parser("list")
    rollback = sub.add_parser("rollback")
    rollback.add_argument("--release-id", required=True)
    args = parser.parse_args()
    if args.command == "backup":
        return command_backup(args.message)
    if args.command == "list":
        return command_list()
    return command_rollback(args.release_id)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("P0 release operation failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
