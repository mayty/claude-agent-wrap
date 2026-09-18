# This file has been created with the assistance of an AI tool.
"""Constants for the litellm_anthropic_sub provider subpackage."""

#: Request header that pins the *upstream* Accept-Encoding to gzip.
#:
#: Claude Code advertises "gzip, deflate, br, zstd" and the passthrough forwards that
#: verbatim, but LiteLLM ships neither `brotli` nor `zstandard` — and httpx *silently*
#: falls back to IdentityDecoder for an encoding it cannot decode rather than raising. A
#: br/zstd reply therefore stays compressed: the agent gets binary labelled
#: `application/json`, and the sidecar's success logging dies on a UnicodeDecodeError, so
#: the request leaves no usage record.
#:
#: The "x-pass-" prefix is what makes this stick. LiteLLM's forward_headers_from_request()
#: merges the client's headers first and assigns the de-prefixed "x-pass-" ones after, so
#: this beats what Claude Code sent; a plain "accept-encoding" here could not.
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

#: Path prefix that lands Claude Code's "${ANTHROPIC_BASE_URL}/v1/messages" on LiteLLM's
#: Anthropic *passthrough* route instead of its translating /v1/messages one.
#:
#: Load-bearing, not cosmetic. Two of the translating endpoint's rewrites strip every
#: marker identifying the traffic as first-party Claude Code:
#:
#:   1. update_headers_with_filtered_beta() drops "claude-code-20250219", which is absent
#:      from LiteLLM's allowlist.
#:   2. _filter_billing_headers_from_system() deletes the leading
#:      "x-anthropic-billing-header" system block.
#:
#: Anthropic's subscription-OAuth gate answers unidentified traffic with an opaque 429.
#: Ordinary agent requests survive on their "You are Claude Code, ..." system block, but
#: the auto-approval classifier carries no such block, so every classifier call failed.
#:
#: One request this cannot rescue: the session-start quota probe sends no system array at
#: all and 429s harmlessly every session — see the provider README before treating that
#: record as a fault.
PASSTHROUGH_PREFIX = "/anthropic"
