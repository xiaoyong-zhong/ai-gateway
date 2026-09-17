FROM docker.litellm.ai/berriai/litellm@sha256:c8756e7b9a61fe45df2ccb5b781d388c3b2f3a21ef9e4956630caef20f9f03aa

COPY gateway_responses_compat.py /app/gateway_responses_compat.py
COPY patch_streaming.py test_empty_choices.py test_gateway_responses_compat.py /opt/gateway-patches/
RUN /app/.venv/bin/python /opt/gateway-patches/patch_streaming.py \
    && LITELLM_LOCAL_MODEL_COST_MAP=True /app/.venv/bin/python /opt/gateway-patches/test_empty_choices.py \
    && PYTHONPATH=/app LITELLM_LOCAL_MODEL_COST_MAP=True /app/.venv/bin/python /opt/gateway-patches/test_gateway_responses_compat.py
