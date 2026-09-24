"""P1 stateless Consumer -> LiteLLM Virtual Key mapper.

It intentionally contains no LiteLLM management credential, database client or
request-body handling. A successful ext-auth response adds only an internal
header which Higress ai-proxy consumes on its next hop.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


APP_ID_PATTERN = re.compile(r"^app-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DOMAIN_SEPARATOR = b"campus-ai-gateway/litellm-key/v1/"
INTERNAL_REQUEST_HEADER = "x-p1-mapper-request"
INTERNAL_REQUEST_VALUE = "p1-ext-auth"
LITELLM_KEY_HEADER = "Authorization"


def read_secret() -> bytes:
    secret_path = Path(os.environ.get("P1_DERIVATION_SECRET_FILE", "/run/secrets/p1_key_derivation_secret"))
    secret = secret_path.read_bytes().strip()
    if len(secret) < 32:
        raise RuntimeError("P1 derivation secret must contain at least 32 bytes")
    return secret


def read_allowlist() -> frozenset[str]:
    apps_path = Path(os.environ.get("P1_APPS_FILE", "/app/apps.json"))
    # Windows PowerShell 5.1 writes JSON with a UTF-8 BOM when using
    # Set-Content -Encoding UTF8. Accept both BOM and BOM-less manifests so a
    # failed/retried local application transaction cannot take the mapper down.
    document = json.loads(apps_path.read_text(encoding="utf-8-sig"))
    app_ids = frozenset(item["app_id"] for item in document["applications"])
    if not app_ids or any(not APP_ID_PATTERN.fullmatch(app_id) for app_id in app_ids):
        raise RuntimeError("P1 apps manifest contains an invalid app_id")
    return app_ids


SECRET = read_secret()
ALLOWLIST = read_allowlist()


def derive_key_from_secret(secret: bytes, app_id: str) -> str:
    digest = hmac.new(secret, DOMAIN_SEPARATOR + app_id.encode("ascii"), hashlib.sha256).digest()
    return "sk-" + base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def derive_key(app_id: str) -> str:
    return derive_key_from_secret(SECRET, app_id)


class MapperHandler(BaseHTTPRequestHandler):
    server_version = "p1-mapper"
    sys_version = ""

    def log_message(self, _format: str, *_args: object) -> None:
        # Deliberately suppress default access logging: it may include client
        # Authorization data in a malformed request target.
        return

    def do_GET(self) -> None:
        if self.path != "/healthz":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        if self.path != "/authorize":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if self.headers.get(INTERNAL_REQUEST_HEADER) != INTERNAL_REQUEST_VALUE:
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        consumer = self.headers.get("x-mse-consumer", "")
        print(f"authorize consumer={consumer or '<missing>'}", flush=True)
        if consumer not in ALLOWLIST:
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        key = derive_key(consumer)
        self.send_response(HTTPStatus.OK)
        self.send_header(LITELLM_KEY_HEADER, "Bearer " + key)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()


def main() -> None:
    address = (os.environ.get("P1_MAPPER_BIND", "0.0.0.0"), int(os.environ.get("P1_MAPPER_PORT", "8081")))
    ThreadingHTTPServer(address, MapperHandler).serve_forever()


if __name__ == "__main__":
    main()
