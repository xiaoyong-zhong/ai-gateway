"""Model-scoped request adaptation for the Qwen gateway's single-system template."""

from typing import Any

from litellm.integrations.custom_logger import CustomLogger


def merge_leading_instructions(data: dict[str, Any]) -> dict[str, Any]:
    inputs = data.get("input")
    if not isinstance(inputs, list):
        return data
    instructions = data.get("instructions")
    if instructions is not None and not isinstance(instructions, str):
        return data
    parts = [instructions] if instructions else []
    count = 0
    for item in inputs:
        if not isinstance(item, dict) or item.get("role") not in ("system", "developer"):
            break
        if item.get("type", "message") != "message":
            break
        content = item.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list) and all(
            isinstance(part, dict) and part.get("type") in ("input_text", "text")
            and isinstance(part.get("text"), str) for part in content
        ):
            parts.extend(part["text"] for part in content)
        else:
            return data
        count += 1
    if count == 0:
        return data
    return {**data, "instructions": "\n\n".join(parts), "input": inputs[count:]}


class QwenResponsesCompatibility(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type != "aresponses" or data.get("model") != "my-qwen3.6-27b":
            return data
        return merge_leading_instructions(data)


callback = QwenResponsesCompatibility()
