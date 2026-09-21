"""Apply the Responses empty-choices fix only to the audited LiteLLM build."""

import hashlib
import sysconfig
from pathlib import Path


EXPECTED_SHA256 = "9ef2cb4de562036bbfe18ba47aa20d37abed270f1e2cb21e94e21979a7688f0d"
MODULE = Path(sysconfig.get_paths()["purelib"]) / (
    "litellm/responses/litellm_completion_transformation/streaming_iterator.py"
)
REPLACEMENTS = (
    (
        "        if self.sent_output_item_added_event:\n"
        "            return\n"
        "        delta: Final = chunk.choices[0].delta\n",
        "        if self.sent_output_item_added_event or not chunk.choices:\n"
        "            return\n"
        "        delta: Final = chunk.choices[0].delta\n",
    ),
    (
        "    def _is_reasoning_end(self, chunk):\n"
        "        delta: Final = chunk.choices[0].delta\n",
        "    def _is_reasoning_end(self, chunk):\n"
        "        if not chunk.choices:\n"
        "            return False\n"
        "        delta: Final = chunk.choices[0].delta\n",
    ),
    (
        "        choice: Final = choices[0]\n"
        "        chat_completion_delta: Final[ChatCompletionDelta] = choice.delta\n",
        "        if not choices:\n"
        "            return \"\"\n"
        "        choice: Final = choices[0]\n"
        "        chat_completion_delta: Final[ChatCompletionDelta] = choice.delta\n",
    ),
)


def main() -> None:
    original = MODULE.read_bytes()
    if hashlib.sha256(original).hexdigest() != EXPECTED_SHA256:
        raise RuntimeError("Unrecognized LiteLLM source; re-audit before applying this patch")
    patched = original.decode("utf-8")
    for before, after in REPLACEMENTS:
        if patched.count(before) != 1:
            raise RuntimeError("Patch anchor is not unique; refusing to modify LiteLLM")
        patched = patched.replace(before, after, 1)
    compile(patched, str(MODULE), "exec")
    MODULE.write_bytes(patched.encode("utf-8"))
    print("Applied Responses empty-choices guards (output item + reasoning boundary + text delta)")


if __name__ == "__main__":
    main()
