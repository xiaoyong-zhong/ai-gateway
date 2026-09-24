"""Request adaptation for providers that require a single system message."""

from typing import Any

from litellm.integrations.custom_logger import CustomLogger


CHAT_CALL_TYPES = {
    "acompletion",
    "completion",
    "anthropic_messages",
    "aanthropic_messages",
}


def _text_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(
        isinstance(part, dict)
        and part.get("type") in ("text", "input_text")
        and isinstance(part.get("text"), str)
        for part in content
    ):
        return "\n".join(part["text"] for part in content)
    return None


def merge_chat_system_messages(data: dict[str, Any]) -> dict[str, Any]:
    messages = data.get("messages")
    if not isinstance(messages, list):
        return data

    system_parts: list[str] = []
    remaining: list[Any] = []
    for message in messages:
        if not isinstance(message, dict):
            return data
        if message.get("role") in ("system", "developer"):
            content = _text_content(message.get("content"))
            if content is None:
                return data
            system_parts.append(content)
        else:
            remaining.append(message)

    if not system_parts:
        return data
    normalized = [{"role": "system", "content": "\n\n".join(system_parts)}]
    normalized.extend(remaining)
    return {**data, "messages": normalized}


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


class GatewayRequestCompatibility(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type == "aresponses":
            return merge_leading_instructions(data)
        if call_type in CHAT_CALL_TYPES:
            return merge_chat_system_messages(data)
        return data


callback = GatewayRequestCompatibility()
