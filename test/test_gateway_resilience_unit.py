"""离线验证异常判定、SSE、并发上限和报告统计，不调用真实模型。"""

import contextlib
from email.message import Message
import io
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import test_gateway_resilience as gateway


class Response(io.BytesIO):
    def __init__(self, body, status=200, stream=False, server="envoy"):
        super().__init__(body.encode("utf-8"))
        self.code = status
        self.headers = Message()
        self.headers["Server"] = server
        self.headers["Content-Type"] = "text/event-stream" if stream else "application/json"


class ResilienceTests(unittest.TestCase):
    def perform(self, response, expected={200}, stream=False):
        with patch.object(gateway, "build_opener") as factory:
            factory.return_value.open.return_value = response
            return gateway.perform("http://localhost:8080/v1", "secret", {}, 1, expected, stream)

    def test_error_status_and_redaction(self):
        headers = Message()
        headers["Server"] = "envoy"
        for status, passed in ((401, True), (500, False)):
            with self.subTest(status=status), patch.object(gateway, "build_opener") as factory:
                factory.return_value.open.side_effect = HTTPError(
                    "http://localhost", status, "error", headers, io.BytesIO(b'error secret'))
                result = gateway.perform("http://localhost:8080/v1", "secret", {}, 1, {401, 403})
                self.assertEqual(result["passed"], passed)
                self.assertEqual(result["status"], status)
                self.assertNotIn("secret", result["error"])

    def test_chat_validity_and_gateway_header(self):
        good = json.dumps({"choices": [{"message": {"content": "OK"}}]})
        self.assertTrue(self.perform(Response(good))["passed"])
        for body in ('{}', 'not-json', '{"choices":[{"message":{"content":""}}]}'):
            self.assertFalse(self.perform(Response(body))["passed"])
        self.assertFalse(self.perform(Response(good, server="uvicorn"))["passed"])

    def test_stream_completion_and_first_text(self):
        chunk = {"choices": [{"index": 0, "delta": {"content": "OK"}, "finish_reason": "stop"}]}
        body = "data: " + json.dumps(chunk) + "\n\n"
        result = self.perform(Response(body + "data: [DONE]\n\n", stream=True), stream=True)
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["first_text_s"], 0)
        self.assertFalse(self.perform(Response(body, stream=True), stream=True)["passed"])
        self.assertFalse(self.perform(Response(body), stream=True)["passed"])
        self.assertFalse(self.perform(Response('data: {"error":"failed"}\n\n', stream=True), stream=True)["passed"])

    def test_missing_answer_diagnostics_and_budget_hint(self):
        for finish in ('stop', 'length', 'content_filter', 'tool_calls'):
            with self.subTest(finish=finish):
                body = {'id': 'response-secret', 'model': 'chat', 'choices': [{
                    'finish_reason': finish,
                    'message': {'content': '', 'reasoning_content': 'private reasoning secret',
                                'tool_calls': [{'function': {'arguments': 'private args'}}]}}],
                    'usage': {'prompt_tokens': 7, 'completion_tokens': 12, 'total_tokens': 19,
                              'completion_tokens_details': {'reasoning_tokens': 12}}}
                result = self.perform(Response(json.dumps(body)))
                self.assertFalse(result['passed'])
                details = result['response_diagnostics']
                self.assertEqual(details['finish_reason'], finish)
                self.assertEqual(details['content_chars'], 0)
                self.assertEqual(details['reasoning_content_chars'], 24)
                self.assertEqual(details['tool_calls_count'], 1)
                self.assertEqual(details['reasoning_tokens'], 12)
                self.assertEqual('Output was truncated' in result['error'], finish == 'length')
                for sensitive in ('secret', 'private reasoning', 'private args'):
                    self.assertNotIn(sensitive, json.dumps(result))

    def test_missing_answer_content_shapes(self):
        for content in (None, '', '   ', [], [{'type': 'text', 'text': 'OK'}]):
            with self.subTest(content=content):
                body = {'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}
                result = self.perform(Response(json.dumps(body)))
                self.assertFalse(result['passed'])
                self.assertEqual(result['response_diagnostics']['content_type'], type(content).__name__)

    def test_timeout_and_percentiles(self):
        with patch.object(gateway, "build_opener") as factory:
            factory.return_value.open.side_effect = TimeoutError("timed out")
            result = gateway.perform("http://localhost:8080/v1", "secret", {}, 1, {200})
            self.assertFalse(result["passed"])
            self.assertIsNone(result["status"])
        self.assertIsNone(gateway.latency_summary([]))
        self.assertEqual(gateway.latency_summary([4, 1, 3, 2])["p50_s"], 2)
        self.assertEqual(gateway.latency_summary([4, 1, 3, 2])["p95_s"], 4)

    def test_concurrency_limit_report_and_failed_status(self):
        lock = threading.Lock()
        active, peak, calls = 0, 0, 0

        def fake_perform(*args, **kwargs):
            nonlocal active, peak, calls
            with lock:
                active += 1
                peak = max(peak, active)
                calls += 1
                number = calls
            time.sleep(0.02)
            with lock:
                active -= 1
            return {"passed": number != 3, "status": 429 if number == 3 else 200,
                    "error": "", "latency_s": 0.02, "first_text_s": None}

        with TemporaryDirectory() as temp:
            argv = ['test', '--model', 'chat', '--suite', 'load', '--requests', '4',
                    '--concurrency', '2', '--report-dir', temp]
            with patch.object(sys, 'argv', argv), patch.object(gateway, 'perform', side_effect=fake_perform), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(gateway.main(), 1)
            report = json.loads(next(Path(temp).glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual(calls, 5)
        self.assertEqual(peak, 2)
        self.assertEqual(report['summary']['passed'], 3)
        self.assertEqual(report['summary']['success_rate'], 0.75)
        self.assertEqual(report['summary']['status_counts'], {'200': 3, '429': 1})
        self.assertEqual(report['summary']['success_latency']['count'], 3)


if __name__ == '__main__':
    unittest.main()
