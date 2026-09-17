import asyncio
import copy
import unittest

from gateway_responses_compat import callback


class QwenCompatibilityTests(unittest.TestCase):
    def apply(self, data, call_type="aresponses"):
        return asyncio.run(callback.async_pre_call_hook(None, None, data, call_type))

    def request(self):
        return {
            "model": "my-qwen3.6-27b",
            "instructions": "Base instructions",
            "input": [
                {"type": "message", "role": "developer", "content": [
                    {"type": "input_text", "text": "First rule"},
                    {"type": "input_text", "text": "Second rule"},
                ]},
                {"role": "user", "content": "Question"},
            ],
            "reasoning": {"effort": "high", "summary": "auto"},
            "tools": [{"type": "function", "name": "probe"}],
        }

    def test_merge_preserves_content_order_and_other_fields(self):
        request = self.request()
        original = copy.deepcopy(request)
        result = self.apply(request)
        self.assertEqual(result["instructions"], "Base instructions\n\nFirst rule\n\nSecond rule")
        self.assertEqual(result["input"], original["input"][1:])
        self.assertEqual(result["reasoning"], original["reasoning"])
        self.assertEqual(result["tools"], original["tools"])
        self.assertEqual(request, original)
        self.assertEqual(self.apply(result), result)

    def test_chat_completions_and_other_call_types_are_untouched(self):
        for call_type in ("acompletion", "completion", "aembedding", "aresponses_compact"):
            request = self.request()
            self.assertIs(self.apply(request, call_type), request)

    def test_other_models_are_untouched(self):
        request = self.request()
        request["model"] = "my-kimi-k2.7-code"
        self.assertIs(self.apply(request), request)

    def test_plain_input_is_untouched(self):
        request = self.request()
        request["input"] = "Question"
        self.assertIs(self.apply(request), request)

    def test_nontext_instruction_is_not_dropped(self):
        request = self.request()
        request["input"][0]["content"].append({"type": "input_image", "image_url": "test"})
        self.assertIs(self.apply(request), request)

    def test_only_leading_instruction_messages_are_merged(self):
        request = self.request()
        later = {"role": "developer", "content": "Later instructions"}
        request["input"].append(later)
        self.assertEqual(self.apply(request)["input"][-1], later)

    def test_multiple_leading_roles_and_missing_instructions(self):
        request = self.request()
        request.pop("instructions")
        request["input"].insert(0, {"role": "system", "content": "System rule"})
        result = self.apply(request)
        self.assertEqual(result["instructions"], "System rule\n\nFirst rule\n\nSecond rule")
        self.assertEqual(len(result["input"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
