"""Offline behavioral regression tests against the actual installed LiteLLM."""

import asyncio
import unittest
from collections.abc import Iterator
from unittest.mock import MagicMock

from litellm.responses.litellm_completion_transformation.streaming_iterator import (
    LiteLLMCompletionStreamingIterator,
)
from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices, Usage


def chunk(*, empty=False, content=None, reasoning=None, finish=None, tools=None, usage=None):
    return ModelResponseStream(
        id="chatcmpl-empty-choices-regression",
        created=1789090000,
        model="my-qwen3.6-27b",
        choices=[] if empty else [StreamingChoices(
            index=0,
            delta=Delta(role="assistant", content=content, reasoning_content=reasoning, tool_calls=tools),
            finish_reason=finish,
        )],
        **({"usage": usage} if usage is not None else {}),
    )


def usage_chunk():
    return chunk(empty=True, usage=Usage(prompt_tokens=11, completion_tokens=7, total_tokens=18))


class FakeStream:
    def __init__(self, chunks: list[ModelResponseStream], fail: bool = False):
        self.chunks: Iterator[ModelResponseStream] = iter(chunks)
        self.logging_obj = MagicMock()
        self.fail = fail

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.chunks)
        except StopIteration:
            if self.fail:
                raise RuntimeError("upstream disconnected")
            raise

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return self.__next__()
        except StopIteration:
            raise StopAsyncIteration from None


def iterator(chunks, fail=False):
    return LiteLLMCompletionStreamingIterator(
        model="my-qwen3.6-27b",
        litellm_custom_stream_wrapper=FakeStream(chunks, fail),
        request_input="Reply OK",
        responses_api_request={},
        custom_llm_provider="openai",
    )


async def collect_async(stream):
    return [event async for event in stream]


def collect(chunks, mode):
    stream = iterator(chunks)
    events = list(stream) if mode == "sync" else asyncio.run(collect_async(stream))
    return events, stream


class EmptyChoicesTests(unittest.TestCase):
    def assert_completed(self, events):
        completed = [event for event in events if event.type == "response.completed"]
        self.assertEqual(len(completed), 1)
        self.assertEqual(events[-1].type, "response.completed")
        response = completed[0].response
        self.assertEqual(response.status, "completed")
        self.assertEqual(response.usage.input_tokens, 11)
        self.assertEqual(response.usage.output_tokens, 7)
        self.assertEqual(response.usage.total_tokens, 18)
        ids = [e.response.id for e in events if e.type in (
            "response.created", "response.in_progress", "response.completed"
        )]
        self.assertEqual(len(set(ids)), 1)
        return response

    def test_leading_middle_and_trailing_empty_chunks(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                chunks = [chunk(empty=True), chunk(content=""), chunk(content="O"),
                          chunk(empty=True), chunk(content="K"), chunk(finish="stop"), usage_chunk()]
                events, stream = collect(chunks, mode)
                response = self.assert_completed(events)
                text = "".join(e.delta for e in events if e.type == "response.output_text.delta")
                self.assertEqual(text, "OK")
                self.assertEqual(response.output[-1].content[0].text, "OK")
                self.assertEqual(len(stream.collected_chat_completion_chunks), len(chunks))

    def test_empty_chunk_does_not_start_an_output_item(self):
        stream = iterator([])
        stream._ensure_output_item_for_chunk(usage_chunk())
        self.assertFalse(stream.sent_output_item_added_event)
        self.assertEqual(stream._pending_response_events, [])

    def test_empty_chunk_does_not_end_reasoning(self):
        stream = iterator([])
        self.assertFalse(stream._is_reasoning_end(usage_chunk()))

    def test_usage_chunk_during_reasoning(self):
        events, stream = collect([
            chunk(reasoning="Think "), usage_chunk(), chunk(reasoning="carefully."),
            chunk(content="OK"), chunk(finish="stop"), usage_chunk(),
        ], "async")
        self.assert_completed(events)
        reasoning = [e for e in events if e.type == "response.reasoning_summary_text.done"]
        self.assertEqual(len(reasoning), 1)
        self.assertEqual(reasoning[0].text, "Think carefully.")
        self.assertTrue(stream._reasoning_done_emitted)

    def test_tool_stream_preserves_arguments(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                events, _ = collect([
                    chunk(empty=True),
                    chunk(tools=[{"index": 0, "id": "call_probe", "type": "function",
                                  "function": {"name": "gateway_probe", "arguments": ""}}]),
                    chunk(empty=True),
                    chunk(tools=[{"index": 0, "function": {"arguments": '{"value":"OK"}'}}]),
                    chunk(finish="tool_calls"), usage_chunk(),
                ], mode)
                response = self.assert_completed(events)
                calls = [item for item in response.output if item.type == "function_call"]
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].name, "gateway_probe")
                self.assertEqual(calls[0].arguments, '{"value":"OK"}')
                args = "".join(e.delta for e in events if e.type == "response.function_call_arguments.delta")
                self.assertEqual(args, '{"value":"OK"}')

    def test_real_upstream_error_is_not_reported_as_completed(self):
        for mode in ("sync", "async"):
            with self.subTest(mode=mode):
                stream = iterator([chunk(empty=True), chunk(content="partial")], fail=True)
                events = []

                async def consume():
                    async for event in stream:
                        events.append(event)

                with self.assertRaisesRegex(RuntimeError, "upstream disconnected"):
                    if mode == "sync":
                        events.extend(stream)
                    else:
                        asyncio.run(consume())
                self.assertNotIn("response.completed", [e.type for e in events])


if __name__ == "__main__":
    unittest.main(verbosity=2)
