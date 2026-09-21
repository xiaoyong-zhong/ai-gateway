FROM docker.litellm.ai/berriai/litellm@sha256:c8756e7b9a61fe45df2ccb5b781d388c3b2f3a21ef9e4956630caef20f9f03aa

COPY callback/qwen_responses_compat.py /app/qwen_responses_compat.py
COPY patch/streaming_empty_choices.py test/test_streaming.py test/test_qwen_compat.py /opt/gateway-patches/
RUN /app/.venv/bin/python /opt/gateway-patches/streaming_empty_choices.py \
    && LITELLM_LOCAL_MODEL_COST_MAP=True /app/.venv/bin/python /opt/gateway-patches/test_streaming.py \
    && PYTHONPATH=/app LITELLM_LOCAL_MODEL_COST_MAP=True /app/.venv/bin/python /opt/gateway-patches/test_qwen_compat.py