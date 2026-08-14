# This file has been created with the assistance of an AI tool.
"""Constants for the litellm_anthropic_sub provider subpackage."""

#: Request header (and value) that pins the *upstream* Accept-Encoding to gzip.
#:
#: Claude Code advertises "accept-encoding: gzip, deflate, br, zstd", and the
#: passthrough route forwards the client's headers verbatim (_forward_headers=True), so
#: that reaches Anthropic unchanged. But LiteLLM ships neither `brotli` nor `zstandard`,
#: and httpx drops "br"/"zstd" from SUPPORTED_DECODERS when those libraries are missing —
#: whereupon Response._get_content_decoder *silently* falls back to IdentityDecoder for
#: them rather than raising. A brotli- or zstd-encoded reply therefore stays compressed in
#: `response.content`, and both halves of the hop break:
#:
#:   1. The agent receives those compressed bytes with `content-encoding` stripped (the
#:      passthrough always strips it), i.e. binary labelled `application/json`.
#:   2. The sidecar's success logging dies inside `transform_response` with "AnthropicError:
#:      Unable to get json response - 'utf-8' codec can't decode byte ...", so the request
#:      leaves no usage record. On the pinned version that surfaces as a bare "Task
#:      exception was never retrieved" traceback in the sidecar log.
#:
#: Restricting the upstream hop to gzip keeps every reply inside what httpx can actually
#: decode. Nothing downstream notices: the passthrough never forwards `content-encoding` to
#: the agent, so Anthropic's choice of encoding was already invisible there.
#:
#: The "x-pass-" prefix is what makes this stick, and is not decoration. LiteLLM's
#: forward_headers_from_request() merges the client's headers first
#: ({**request_headers, **custom_headers}) and only *then* assigns the de-prefixed
#: "x-pass-" ones, so this beats the accept-encoding Claude Code sent. Setting a plain
#: "accept-encoding" here could not: that header belongs to Claude Code's own HTTP layer
#: on the agent→sidecar hop, which is free to overwrite it. Anthropic also receives the
#: undecorated "x-pass-accept-encoding" alongside it (forwarded with everything else) and
#: ignores it, as it does any unknown x-* header.
ACCEPT_ENCODING_OVERRIDE_HEADER = "x-pass-accept-encoding"

#: Value for ACCEPT_ENCODING_OVERRIDE_HEADER. Deliberately gzip alone: it is the one
#: encoding httpx decodes without an optional dependency (deflate too, but it buys
#: nothing), so this must never grow "br" or "zstd" back — those are the failure.
ACCEPT_ENCODING_OVERRIDE_VALUE = "gzip"

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
