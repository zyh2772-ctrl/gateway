# Local LLM Stack Status

Date: 2026-04-04

## Working

- `llama.cpp` standalone servers are running for:
  - `qwen-deep` on `127.0.0.1:18085`
  - `qwen-fast` on `127.0.0.1:18081`
  - `qwen-extract` on `127.0.0.1:18082`
  - `code-fast` on `127.0.0.1:18083`
  - `embed-m3` on `127.0.0.1:18084`
- Additional `llama.cpp` servers are running for:
  - `huihui-27b` on `127.0.0.1:18088`
  - `gemma-4-26b` on `127.0.0.1:18089`
- LiteLLM is serving the unified gateway on `127.0.0.1:4000`
- The new stack supervisor is serving status on `127.0.0.1:4060`
- The supervisor dashboard is available on `http://127.0.0.1:4060/`
- The supervisor control API is available on `POST http://127.0.0.1:4060/control`
- The supervisor profile API is available on `POST http://127.0.0.1:4060/profile`
- The supervisor probe API is available on `POST http://127.0.0.1:4060/probe`
- Verified endpoints through LiteLLM:
  - `POST /v1/chat/completions` with `qwen-fast`
  - `POST /v1/embeddings` with `embed-m3`
  - `POST /v1/responses` with `qwen-fast`
  - streaming `POST /v1/responses` with `qwen-fast`
  - `POST /v1/chat/completions` with `huihui-27b`
  - `POST /v1/chat/completions` with `gemma-4-26b`
  - `POST /v1/chat/completions` with `uncensored-fallback`
  - `POST /v1/chat/completions` with `gemma-fallback`
  - `POST /v1/responses` with `huihui-27b`
  - `POST /v1/responses` with `gemma-4-26b`
- Verified resilience behavior:
  - supervisor adopts already-running `huihui-27b`, `gemma-4-26b`, and `litellm`
  - killing `huihui-27b` is detected and the service is automatically restarted
  - after restart, `huihui-27b` health and gateway inference recover successfully
  - the built-in dashboard renders correctly from the supervisor root path
  - start and stop actions work through the new supervisor control API
  - profile switching works through the new supervisor profile API
  - model probe works through the new supervisor probe API
  - latest probe result now persists per service and is reflected in `/status`
  - the dashboard now shows profile composition, probe detail, and unified API examples for the selected service
  - the launchd installer script successfully renders a LaunchAgent plist in a sandboxed test home
- Alias routing configured:
  - `uncensored-fallback -> huihui-27b`
  - `gemma-fallback -> gemma-4-26b`

## Local Patches Applied

- `litellm-main/litellm/responses/main.py`
  - added `model_info.responses_via_chat_completions: true` override support
- `litellm-main/litellm/llms/openai/openai.py`
  - filters `None` embedding params before calling stricter local OpenAI-compatible backends
- `litellm-main/litellm/responses/litellm_completion_transformation/transformation.py`
  - fixes bridged Responses objects to use top-level `"object": "response"`

## Current Limitation

- `huihui-27b` and `gemma-4-26b` have been added to config, but whether both should stay resident at the same time depends on host memory pressure.
- Ollama runtime may still be used separately for compatibility, but it is no longer on the critical path for these two large fallback aliases.

## Current API

- Base URL: `http://127.0.0.1:4000`
- Bearer token: `sk-local-gateway`
- Supervisor status URL: `http://127.0.0.1:4060/status`
- Supervisor dashboard URL: `http://127.0.0.1:4060/`
- Supervisor control URL: `http://127.0.0.1:4060/control`
- Supervisor profile URL: `http://127.0.0.1:4060/profile`
- Supervisor probe URL: `http://127.0.0.1:4060/probe`

## Recommended Next Action

- Keep `llama.cpp` as the production path now.
- Run the stack through `start_stack_supervisor.sh` so model and gateway health stay under one supervisor instead of ad hoc manual launches.
- If you want true reboot-level continuity on macOS, the next operational step is loading the generated LaunchAgent into `launchctl`.
