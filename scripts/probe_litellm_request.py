import asyncio
import json
import os
from pathlib import Path

import litellm
from litellm.integrations.custom_logger import CustomLogger


class Capture(CustomLogger):
    def log_pre_api_call(self, model, messages, kwargs):
        body = kwargs.get('additional_args', {}).get('complete_input_dict', {})
        print('API payload keys:', sorted(body), flush=True)
        print('Options:', json.dumps({k: body.get(k) for k in (
            'reasoning_effort', 'parallel_tool_calls', 'web_search_options', 'stream', 'extra_body'
        )}, default=str)[:3000], flush=True)
        Path('/tmp/litellm-upstream-request.json').write_text(json.dumps(body), encoding='utf-8')


async def main():
    litellm.callbacks = [Capture()]
    litellm.drop_params = True
    request = json.loads(Path('/tmp/codex-request.bin').read_bytes())
    request['model'] = 'openai/Qwen3.6-27B'
    request['reasoning'] = {'effort': 'high'}
    request['api_base'] = 'https://test2-aigc.campusapp.com.cn/api/v1'
    request['api_key'] = os.environ['ZHILIN_aigc_API_KEY']
    request['use_chat_completions_api'] = True
    result = await litellm.aresponses(**request)
    async for event in result:
        if event.type == 'response.output_text.delta':
            print('TEXT:', event.delta, flush=True)
        if event.type == 'response.completed':
            print('Usage:', event.response.usage, flush=True)


asyncio.run(main())
