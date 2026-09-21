"""Inspect only the target provider's error envelope, without logging credentials."""

import json
import os
from pathlib import Path

import httpx
from litellm.responses.litellm_completion_transformation.transformation import LiteLLMCompletionResponsesConfig

request = json.loads(Path('/tmp/codex-request.bin').read_bytes())
optional = {k: v for k, v in request.items() if k not in ('model', 'input', 'stream')}
body = LiteLLMCompletionResponsesConfig.transform_responses_api_request_to_chat_completion_request(
    model='Qwen3.6-27B', input=request['input'], responses_api_request=optional,
    custom_llm_provider='openai', stream=False,
)
print('Transformed keys:', sorted(body), flush=True)
print('Message shapes:', [(m.get('role'), type(m.get('content')).__name__) for m in body['messages']], flush=True)
body.pop('custom_llm_provider', None)
body['reasoning_effort'] = 'high'
body['stream'] = True
body['stream_options'] = {'include_usage': True}
body = json.loads(Path('/tmp/litellm-upstream-request.json').read_text())
body.update(body.pop('extra_body', {}))
body['stream'] = False
body.pop('stream_options', None)
with httpx.Client(timeout=60) as client:
    response = client.post('https://test2-aigc.campusapp.com.cn/api/v1/chat/completions',
                           headers={'Authorization': 'Bearer ' + os.environ['ZHILIN_aigc_API_KEY']}, json=body)
    print('Status:', response.status_code, 'Content-Type:', response.headers.get('content-type'), flush=True)
    if 'text/event-stream' not in response.headers.get('content-type', ''):
        print(response.text[:3000], flush=True)
    else:
        count = 0
        for line in response.text.splitlines():
            if not line.startswith('data:') or line[5:].strip() == '[DONE]':
                continue
            result = json.loads(line[5:])
            if result.get('code') or result.get('error'):
                print(json.dumps(result, ensure_ascii=True)[:3000], flush=True)
            count += len(result.get('choices') or [])
        print('Choices across stream:', count, flush=True)
        if count == 0:
            print(response.text[:3000], flush=True)
