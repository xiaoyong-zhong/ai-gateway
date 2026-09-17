"""双入口鉴权测试的回归用例，不连接网关或真实模型。"""

import io
import json
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import test_dual_gateway as gateway


class DualGatewayTests(unittest.TestCase):
    def call_with(self, body, status=200, key="sk-unit-secret"):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = json.dumps(body).encode()
        with patch.object(gateway, "build_opener") as factory:
            factory.return_value.open.return_value = response
            result = gateway.call("http://localhost:4000/v1", key, "chat", "OK", 1)
            request = factory.return_value.open.call_args.args[0]
        return result, request

    def test_anonymous_case_omits_authorization(self):
        _, request = self.call_with({}, key=None)
        self.assertIsNone(request.get_header("Authorization"))

    def test_null_content_is_a_valid_auth_response(self):
        result, request = self.call_with({"choices": [{"message": {"content": None}}]})
        self.assertEqual(result["preview"], "")
        self.assertTrue(gateway.auth_passed("authorized", result))
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-unit-secret")

    def test_malformed_responses_do_not_prove_authorization(self):
        for body in [None, [], {"choices": [None]}, {"choices": "bad"},
                     {"error": {"message": "failed"}}]:
            with self.subTest(body=body):
                result, _ = self.call_with(body)
                self.assertFalse(gateway.auth_passed("authorized", result))

    def test_http_error_preserves_evidence_and_redacts_key(self):
        body = json.dumps({"error": {
            "message": "key sk-unit-secret not allowed", "type": "key_model_access_denied",
        }}).encode()
        error = HTTPError("http://localhost:4000/v1", 403, "Forbidden", {}, io.BytesIO(body))
        with patch.object(gateway, "build_opener") as factory:
            factory.return_value.open.side_effect = error
            result = gateway.call("http://localhost:4000/v1", "sk-unit-secret", "chat", "OK", 1)
        self.assertEqual(result["status"], 403)
        self.assertNotIn("sk-unit-secret", json.dumps(result))
        self.assertTrue(gateway.auth_passed("forbidden-model", result))

    def test_wrong_failure_cannot_pass_model_permission_check(self):
        for status, kind in [(404, "model_not_found"), (500, "internal_error"),
                             (401, "provider_authentication_error"), (403, "budget_exceeded")]:
            with self.subTest(status=status, kind=kind):
                self.assertFalse(gateway.auth_passed("forbidden-model", {
                    "status": status, "error": {"type": kind},
                }))

    def test_network_failure_does_not_count_as_denial(self):
        with patch.object(gateway, "build_opener") as factory:
            factory.return_value.open.side_effect = URLError("connection refused")
            result = gateway.call("http://localhost:4000/v1", None, "chat", "OK", 1)
        self.assertFalse(gateway.auth_passed("missing-key", result))


if __name__ == "__main__":
    unittest.main()
