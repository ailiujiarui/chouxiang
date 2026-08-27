# DeepSeek Runtime And Test Isolation Design

Date: 2026-07-25  
Status: implemented; review passed

## Goal

Keep the test suite deterministic and network-free while making the normal
application runtime depend on a real DeepSeek configuration instead of
silently falling back to a mock provider.

## Behavior

### Tests

- Tests that exercise CLI analysis explicitly pass `--mock` or inject a fake
  provider.
- Tests must not inherit a developer's `DEEPSEEK_API_KEY` or call the network.
- Add a regression assertion that the test path reports the deterministic mock
  provider.

### Runtime

- `REFACTOR_AGENT_MOCK_LLM` defaults to `false` in Compose and application
  startup scripts.
- Normal startup requires `DEEPSEEK_API_KEY`; missing credentials fail with a
  clear startup error instead of selecting mock mode.
- An explicit `REFACTOR_AGENT_MOCK_LLM=true` remains available only as an
  intentional offline/demo override, and startup output must label it as demo.
- Real runtime code continues to instantiate `DeepSeekClient`; no test-only
  mock branch is added to production provider code.

## Documentation

Update README startup instructions to distinguish required DeepSeek runtime
configuration from the explicit offline mock override.

## Verification

- Run CLI/config/provider tests with no network calls.
- Validate Compose configuration and startup-mode selection tests.
- Run compile and diff checks.

## Verification result

- CLI/config and CI contract tests pass without a network call.
- Compose defaults to real DeepSeek mode; mock requires an explicit override.
- Startup rejects missing runtime credentials in non-mock mode.
- Compile, Compose validation, and diff checks pass.

## Non-goals

- No changes to DeepSeek prompts, privacy filtering, or provider API payloads.
- No live DeepSeek call in CI or unit tests.
