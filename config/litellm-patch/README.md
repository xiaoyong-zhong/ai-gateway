# Responses Compatibility Patch

This is a project-local compatibility fix, not an upgrade to LiteLLM or a claim
of native Responses support at the upstream provider.

## Scope

- Both `/v1/chat/completions` and `/v1/responses` remain available at Higress.
- Chat Completions keeps its existing request path.
- Models configured with `use_chat_completions_api: true` use LiteLLM's existing
  Responses-to-Chat bridge. This patch does not add another protocol proxy.
- Three bounds checks in the Responses streaming iterator accept empty choices
  without creating text/items or ending reasoning. Usage chunks remain collected.
- `gateway_responses_compat.callback` runs only for `aresponses` and the exact
  model alias `my-qwen3.6-27b`. It merges leading text-only system/developer
  messages into `instructions`, preserving order and leaving user/tool messages
  and other request options unchanged. The upstream Qwen chat template rejects
  a second system message after LiteLLM translates developer roles to system.
- No credentials are copied into the image. The build context is this folder,
  not the project root or `.env`.

## Build and Verify

Run from the repository root:

```powershell
docker compose build litellm
docker compose up -d --no-deps --no-build litellm
Invoke-WebRequest -UseBasicParsing http://localhost:4000/health/liveliness
```

Allow startup to finish before sending requests. Recreating LiteLLM temporarily
interrupts all endpoints served by it, although Higress is not recreated.

The build runs 13 offline tests against the actual installed LiteLLM. The six
streaming tests reproduced IndexError before the guards were applied. The seven
request-hook tests check scope, preservation of content, and unsupported inputs.

The live smoke test makes five billable upstream calls and checks final text,
usage, stream termination, and an automatic function call. Set `GATEWAY_API_KEY`
in the current shell to your existing gateway key, then run:

```powershell
python test/test_responses_patch_live.py
```

Verified on 2026-09-11 with `my-qwen3.6-27b` through
`http://192.168.31.113:8080/v1`:

- All 13 build-time regression tests passed.
- All five live checks passed: Chat nonstream/stream, Responses nonstream/stream
  with leading developer instructions, and Responses automatic function calling.
- VS Code's bundled Codex 0.153.4 returned `GATEWAY_OK` with nonzero usage.
- The same Codex completed a read-only PowerShell tool round trip and returned
  `GATEWAY_TOOL_OK`. Request and stream retries were disabled during both probes.
- Compose rollback configuration validates. Rollback was not deployed for testing.

## Rollback

The original base image is pinned by digest in `Dockerfile`. Its streaming module
must match an audited SHA-256 before patching; a future upstream upgrade requires
re-audit instead of silently applying stale replacements.

To revert only the streaming guards while retaining the Qwen request hook:

```powershell
docker compose -f docker-compose.yml -f config/litellm-patch/compose.rollback.yml up -d --no-deps --no-build litellm
```

For a full rollback, also remove `gateway_responses_compat.callback` from the
`callbacks` list in `config/litellm.yaml` before running the command. The override
mounts the hook so the original image can load the config even when it is retained.
To return to the patched image, run the normal build/deploy commands above without
the rollback override. The historical Codex failures can return after rollback.

## Limits and Risks

- This does not turn Chat-only upstreams into native Responses services.
- Merging system/developer instructions reflects the target template's limitation;
  it cannot preserve distinct system/developer priority as on a native provider.
  Only leading text instructions are merged. Later developer messages and
  non-text instruction content are deliberately not reordered or discarded.
- Hosted tools, encrypted reasoning, stateful response reuse, and all Codex agent
  workflows are not guaranteed. Unknown-model metadata warnings in Codex remain.
- Explicit function-shaped `tool_choice` has a separate known bridge validation
  issue. Automatic function selection is the tested path; this patch does not
  change tool-choice conversion.
- The upstream can wrap application errors in HTTP 200/nonstandard SSE. Empty
  choices are not proof of successful inference. Check nonempty final output or
  a valid tool call as well as the terminal event, not HTTP status alone.
- This is not a load, long-context, or long-duration reliability certification.
  Network faults, rate limits, and model behavior remain external failure modes.
