# This file has been created with the assistance of an AI tool.
"""Constants for the litellm_anthropic_sub provider subpackage."""

#: Header the master key travels on. Must NOT be "Authorization": that header has
#: to stay free to carry the subscription OAuth token (Bearer sk-ant-oat...) which
#: the passthrough route forwards upstream verbatim. Presenting the master key as a
#: bearer token would overwrite the OAuth credential, causing a 401 upstream.
#: LiteLLM authenticates the proxy hop from this header directly (see
#: custom_litellm_key_header in its proxy auth).
MASTER_KEY_HEADER = "x-litellm-api-key"

#: Path prefix appended to the sidecar base URL so Claude Code's own
#: "${ANTHROPIC_BASE_URL}/v1/messages" lands on LiteLLM's Anthropic *passthrough*
#: route (/anthropic/v1/messages) instead of its /v1/messages one.
#:
#: This is load-bearing, not cosmetic. /v1/messages is a translating endpoint that
#: normalizes Anthropic-native requests, and two of its rewrites strip every marker
#: identifying the traffic as first-party Claude Code:
#:
#:   1. update_headers_with_filtered_beta() drops any anthropic-beta value absent
#:      from LiteLLM's allowlist — and "claude-code-20250219" is not in it.
#:   2. _filter_billing_headers_from_system() deletes the leading
#:      "x-anthropic-billing-header: ..." system block Claude Code sends.
#:
#: Anthropic's subscription-OAuth gate answers unidentified traffic with an opaque
#: 429 rate_limit_error / "message":"Error". Ordinary agent requests survive because
#: their next system block ("You are Claude Code, ...") still reads as first-party,
#: but Claude Code's auto-approval classifier carries no such block and relies purely
#: on the header plus billing block — so every classifier call failed. The passthrough
#: route forwards headers and body verbatim, so neither rewrite applies.
#:
#: One request it cannot rescue: Claude Code's session-start quota probe sends no system
#: array at all, so the marker it lacks was never in the body to forward. It 429s on
#: every session start, harmlessly — see the provider README's "Known benign failure"
#: section before treating that record as a fault.
PASSTHROUGH_PREFIX = "/anthropic"
