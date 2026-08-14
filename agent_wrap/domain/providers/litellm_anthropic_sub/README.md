<!-- This file has been edited with the assistance of an AI tool. -->
# LiteLLM Anthropic Sub Provider

Routes Claude Code through Anthropic's own API via its own LiteLLM sidecar — but,
unlike every other provider here, to spend a **claude.ai subscription** rather than
per-token API credits.

## Lifecycle

The sidecar lifecycle is common to all LiteLLM providers — see
[`providers/README.md`](../README.md). Anthropic adds no master-key auto-approval
(unlike DashScope/DeepSeek) because it never sends `ANTHROPIC_API_KEY` for Claude Code
to prompt about in the first place.

## Configuration

| Item | Value |
| --- | --- |
| Image | `ghcr.io/berriai/litellm:v1.96.2@sha256:154e23bb...` |
| Master key prefix | `sk-aw-ant-` |
| Sidecar container | `agent-wrap-litellm-anthropic-sub` |
| Agent base URL | `http://agent-wrap-litellm-anthropic-sub:<port>/anthropic` |
| Upstream endpoint | `https://api.anthropic.com` |

The `/anthropic` suffix is load-bearing — see
[Why the passthrough route](#why-the-passthrough-route) below.

`<port>` is resolved when the sidecar starts (scanning upward from 48620) and recorded
in the container as `AGENT_WRAP_SIDECAR_PORT`, so several providers' sidecars can run
at once — see [`providers/README.md`](../README.md#sidecar-lifecycle).

## Subscription billing

This provider declares no secret and sets no `ANTHROPIC_API_KEY` /
`ANTHROPIC_AUTH_TOKEN`. Anthropic's documented LLM gateway pattern is: point
`ANTHROPIC_BASE_URL` at the gateway and set no credential env var at all — Claude Code
then keeps using whatever credential it already has active (its own claude.ai login)
and sends it upstream itself. Setting either credential env var here would override
that active login with a Console API key, which bills API credits instead of the
subscription — the one thing this provider exists to avoid.

The sidecar's own master key still exists (every LiteLLM sidecar has one, minted at
cold start), but it authenticates the loopback hop between the agent and the sidecar,
never Anthropic. It travels on the `x-litellm-api-key` header, not `Authorization` —
see [`constants.py`](constants.py) for why the header choice is load-bearing, not
cosmetic. `master_key_prefix = "sk-aw-ant-"` is chosen so it can never be confused with
an `sk-ant-oat...` subscription OAuth token.

## Why the passthrough route

Agent traffic goes to LiteLLM's **Anthropic passthrough** route
(`/anthropic/v1/messages`), not its `/v1/messages` route. `ANTHROPIC_BASE_URL` therefore
carries an `/anthropic` suffix, and Claude Code appends `/v1/messages` itself.

`/v1/messages` is a *translating* endpoint: it normalizes Anthropic-native requests, and
two of its rewrites strip every marker that identifies traffic as first-party Claude
Code.

1. `update_headers_with_filtered_beta()` keeps only `anthropic-beta` values present in
   LiteLLM's allowlist. **`claude-code-20250219` is not in it**, so it is dropped from
   every request (as are `redact-thinking-*`, `thinking-token-count-*`,
   `mid-conversation-system-*`, `afk-mode-*` and `extended-cache-ttl-*`).
2. `_filter_billing_headers_from_system()` deletes the leading
   `x-anthropic-billing-header: cc_version=…; cc_entrypoint=cli;` block that Claude Code
   sends as `system[0]`.

Anthropic's subscription-OAuth gate answers traffic it cannot recognize as first-party
with a deliberately opaque `429` — `rate_limit_error` with `"message":"Error"` — which
looks like quota exhaustion but is not (the calls fail in well under a second and
interleave with successes).

Ordinary agent requests survived this because their *next* system block is literally
`"You are Claude Code, Anthropic's official CLI for Claude."` (or `"You are a Claude
agent, built on Anthropic's Claude Agent SDK."` for SDK subagents), which still reads as
first-party once the billing block is gone. Claude Code's **auto-approval classifier**
carries no identity block at all and relies purely on the header plus billing block — so
before this fix *every* classifier call failed while normal traffic worked.

The passthrough route forwards headers and body verbatim, so neither rewrite applies. It
also removes the previous dependence on LiteLLM's `clean_headers` /
`authenticated_with_header` internals: `custom_headers` resolves to `{}` when no
`ANTHROPIC_API_KEY` is in the sidecar env, and the client's `Authorization` header is
merged through untouched.

## Why the upstream Accept-Encoding is pinned

Verbatim header forwarding cuts both ways. Claude Code advertises
`accept-encoding: gzip, deflate, br, zstd`, and because the passthrough forwards the
client's headers untouched, Anthropic sees that same list and may answer with `br` or
`zstd`. LiteLLM cannot read either: it pins httpx but ships neither `brotli` nor
`zstandard`, and httpx removes those two from `SUPPORTED_DECODERS` when the optional
packages are absent. The part that makes this expensive to diagnose is that
`Response._get_content_decoder()` does not raise on an encoding it has no decoder for —
it `continue`s past it and falls back to `IdentityDecoder`, so the compressed bytes are
handed on as if they were plaintext.

Both halves of the hop then break, in ways that look unrelated:

- **The agent** gets those compressed bytes with `content-encoding` stripped off — the
  passthrough always strips it — i.e. binary labelled `application/json`.
- **The sidecar** loses the request's usage record. Its success logging calls
  `transform_response()`, which does `raw_response.json()` and raises
  `AnthropicError: Unable to get json response - 'utf-8' codec can't decode byte 0x..`.
  On the pinned version that logging runs in a bare `asyncio.create_task`, so it surfaces
  as a `Task exception was never retrieved` traceback and nothing else.

`x-pass-accept-encoding: gzip` restricts the upstream hop to the one encoding httpx
decodes unaided. The agent notices nothing either way, since `content-encoding` never
reaches it.

The `x-pass-` prefix is the mechanism, not decoration: LiteLLM's
`forward_headers_from_request()` merges the client's headers first
(`{**request_headers, **custom_headers}`) and only *then* assigns the de-prefixed
`x-pass-` ones, so this entry wins over the `accept-encoding` Claude Code sent. A plain
`accept-encoding` entry would lose that merge — and would be Claude Code's own HTTP
layer's header to overwrite on the agent→sidecar hop regardless. Anthropic also receives
the still-prefixed `x-pass-accept-encoding` alongside the real one and ignores it, as it
does any unknown `x-*` header.

This is not fixed by moving to a newer LiteLLM: as of `v1.96.2` the forwarding, the
missing decoders and the unguarded `raw_response.json()` are all unchanged. What *does*
change there is that passthrough logging moved onto a worker that catches the exception,
so the traceback becomes one logged error — the corrupt response and the missing usage
record remain.

## Known benign failure: the session-start quota probe

Every session's request log opens with one failed `POST /anthropic/v1/messages`. It is
expected, it is not quota exhaustion, and it cannot be fixed here — so before debugging
it, read this.

Claude Code's `checkQuotaStatus()` fires one throwaway request at startup whose only
purpose is to read the `anthropic-ratelimit-unified-*` **response** headers. It is issued
with `maxRetries: 0`, which is why exactly one appears:

```json
{"model": "claude-opus-5", "max_tokens": 1,
 "messages": [{"role": "user", "content": "quota"}]}
```

Note what is missing: there is no `system` array at all. This is the only request shape
Claude Code sends without one, so it carries neither marker described in
[Why the passthrough route](#why-the-passthrough-route) — no `x-anthropic-billing-header`
block and no `"You are Claude Code, …"` identity block. Its *headers* arrive intact
(`anthropic-beta` still lists `claude-code-20250219,oauth-2025-04-20,…`, plus `x-app: cli`
and the forwarded `Authorization`), which is exactly the point: headers alone do not
satisfy Anthropic's subscription-OAuth gate, so it answers with the same opaque
`429 rate_limit_error` / `"message":"Error"`.

That body is not what lands in the log. Since the `v1.96.2` logging change described
[above](#why-the-upstream-accept-encoding-is-pinned), the worker that records the failure
holds only the status by then, so the record reads `429: Upstream passthrough request
failed with status 429` — no `rate_limit_error` type and no upstream `request_id`. Do not
go looking for them; the status is all the sidecar has to give, and it is what the viewer
matches on.

**The passthrough cannot rescue this one.** It forwards the body verbatim, and the marker
this request lacks was never in the body to begin with. Nor can the sidecar add one:
`async_pre_call_hook` is never called on the `/anthropic/*` route (see
[`litellm_runtime/callback.py`](../litellm_runtime/callback.py)), and synthesizing a
billing block to pass a first-party check is not something to do on purpose.

**Nothing is broken by it.** Claude Code's `extractQuotaStatusFromError` returns early in
gateway mode unless the 429 carries an `anthropic-ratelimit-unified-status` header; this
one does not, so the error is discarded and the account is never marked rate-limited. The
only visible effects are cosmetic:

- the statusline's rate-limit bar shows its "send a message to see limits" fallback
  ([`ops/statusline.py`](../../../../ops/statusline.py)) until the first real turn
  populates `rate_limits` from a successful response's headers;
- the log keeps an honest `failure` record, which the viewer draws with a muted `probe`
  badge and one explanatory line instead of a red error bubble
  (`isQuotaProbeRequest` in [`logs_page/app.js`](../../../../logs_page/app.js)). Only the
  drawing is muted — `meta.json` counts, `agent stats` and the on-disk record are
  untouched, and a probe that fails any *other* way (a 404 from a stale model id, say)
  still renders red.

`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` does suppress the probe outright — it is the
first guard in `checkQuotaStatus()`. That is not a trade worth making here: it is the flag
this provider deliberately leaves off, and it also disables the feature-flag evaluation
`/usage` depends on (see [Non-essential traffic stays on](#non-essential-traffic-stays-on)).

## Credentials

There is no encrypted secret to set for this provider — `agent secrets check
litellm-anthropic-sub` reports "declares no secrets". Instead, authenticate once per
machine with a one-time in-container `/login` (the paste-the-code fallback works
without a browser). The resulting OAuth credential is written to
`.claude_config/.claude/.credentials.json`, which is mounted into every project's
container and shared by all of them — so one `/login` covers every project on the
host, not just this one.

## Gateway-mode side effects

Setting `ANTHROPIC_BASE_URL` puts Claude Code into gateway mode, which has a few
side effects worth knowing about so they don't get mistaken for bugs:

- Remote Control is disabled when `ANTHROPIC_BASE_URL` points off-Anthropic.
- The fast-mode check and the WebFetch preflight both bypass the gateway, so they
  will not show up in the request log even though everything else does.
- MCP tool search and fine-grained tool streaming default off.

## Non-essential traffic stays on

Every other provider here sets `disable_nonessential_traffic = True` (the
`Provider` default), which makes `LaunchService._build_env_args` inject
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. That flag also disables Claude
Code's feature-flag evaluation against Anthropic's backend — which gates Remote
Control and other Anthropic-backed feature checks. This provider's whole point is
a live Anthropic-linked session, and its own "Pricing" section above tells users
to check consumption with `/usage` — so this provider overrides
`disable_nonessential_traffic = False` and leaves that evaluation running.
`_build_env_args` substitutes `DISABLE_AUTOUPDATER=1` in its place, so the CLI
baked into the image still never tries to self-update.

## Env vars

Agent container (injected by `get_agent_env`):

- `ANTHROPIC_BASE_URL` — `http://agent-wrap-litellm-anthropic-sub:<port>/anthropic`
  (the `/anthropic` suffix selects the passthrough route — see
  [Why the passthrough route](#why-the-passthrough-route))
- `ANTHROPIC_CUSTOM_HEADERS` — two newline-separated entries:
  - `x-litellm-api-key: <master key>`, so the sidecar's master key reaches the proxy
    without occupying the `Authorization` header, which must stay free for the
    forwarded subscription token
  - `x-pass-accept-encoding: gzip`, which pins the *upstream* Accept-Encoding to
    something LiteLLM can decode — see
    [Why the upstream Accept-Encoding is pinned](#why-the-upstream-accept-encoding-is-pinned)

  The sidecar layer appends a third entry (`x-agent-wrap-log-prefix`) to this value at
  launch; see [`providers/README.md`](../README.md).

Agent container (injected by `LaunchService._build_env_args`, not `get_agent_env`,
because this provider sets `disable_nonessential_traffic = False`):

- `DISABLE_AUTOUPDATER` — `1`, substituted for `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`
  so the launcher still disables auto-update as a side effect

Sidecar container (injected by `get_sidecar_env`): none — there is no upstream
credential for this provider to inject.

## Config

See [`config.yaml`](config.yaml) for the LiteLLM proxy config. The one line every
other provider's config lacks is `forward_client_headers_to_llm_api: true`, which
forwards the client's `anthropic-beta` header and the `Authorization` bearer token
upstream. It governs the `/v1/messages` route, which the agent no longer uses; on the
passthrough route header forwarding is inherent (`_forward_headers=True`). The
`model_list` entry likewise no longer routes this provider's traffic — it exists only
because LiteLLM wants a non-empty list to boot.

### Request logging is registered in three different places

Failures and successes reach the callback by different routes, and only one of them can
be configured in YAML:

| Half | LiteLLM list | Consumed by | Registered by |
| --- | --- | --- | --- |
| failures (non-streaming) | `litellm.callbacks` | `post_call_failure_hook` | `callbacks:` in `config.yaml` |
| failures (streaming) | `litellm._async_failure_callback` | `Logging.async_failure_handler` | `add_litellm_async_failure_callback` in [`callback.py`](../litellm_runtime/callback.py) |
| successes | `litellm._async_success_callback` | `Logging.async_success_handler` | `add_litellm_async_success_callback` in [`callback.py`](../litellm_runtime/callback.py) |

The failure half needed splitting in two. `callbacks:` in `config.yaml` does populate
`litellm.callbacks`, so `post_call_failure_hook` fires — but measured against the on-disk
logs it only ever produced records for **non-streaming** requests. Streaming calls showed
thousands of success records against a single failure record, while non-streaming calls
logged failures normally. Since every real conversation turn streams, upstream errors —
Anthropic's `529 overloaded_error` in particular — were missing from exactly the requests
that matter, and the user saw the request vanish from the session timeline rather than
fail. Registering `_async_failure_callback` closes that half; both hooks funnel through
one writer that dedups on `litellm_call_id`, so the overlap on non-streaming failures
still yields one record.

On the router (`/v1/messages`) path LiteLLM's `function_setup()` copies
`litellm.callbacks` into the per-event lists, so the `callbacks:` key alone was enough.
The `/anthropic/*` passthrough route never calls `function_setup()`, so that list stays
empty and **every successful request goes unlogged while failures still appear**. Because
the log keeps filling with failure records, the fault reads as "the provider is fine"
rather than as broken logging.

There is no config key for the success half, and `success_callback:` is a trap that looks
like one: it routes through `add_litellm_success_callback`, which only reaches
`_async_success_callback` when `_is_async_callable()` is true — and that is False for a
`CustomLogger` *instance*, which has no `__call__`. It lands in the sync
`success_callback` list instead, which passthrough requests skip twice over
(`success_handler` excludes `call_type == "pass_through_endpoint"`, and the sync gate
filters out every `CustomLogger`). Hence the in-code registration.

## Pricing

`agent stats` always reports `$0.00` for this provider's rows — truthfully: a
subscription has no marginal per-token cost, so there is no rate table here to keep in
sync with Anthropic's list prices. Token counts are still tracked. To see actual
subscription consumption, use `/usage` inside Claude Code or your usage view on
claude.ai — not `agent stats`.

## Pinned dependency

Do not bump `LITELLM_IMAGE` for this provider without re-verifying that
`/anthropic/{endpoint}` in the pinned LiteLLM version still forwards headers **and**
body verbatim — specifically that the inbound `Authorization: Bearer sk-ant-oat...`
header, the `anthropic-beta: …claude-code-20250219…` header and the leading
`x-anthropic-billing-header` system block all reach Anthropic unmodified. That route's
behavior is LiteLLM internals, not a supported contract, and a version bump could
silently change it — into a loud 401 (safe), a quiet change of which credential reaches
Anthropic (not safe), or a return of the opaque 429 on classifier calls (not safe).

Confirm both halves, because a passing normal request does **not** imply a passing
classifier request:

1. Make one ordinary request and check it 200s.
2. Trigger an auto-approval classifier call and check it 200s too.
3. In the session log, confirm `request.headers.anthropic-beta` still contains
   `claude-code-20250219` and `request.body.system[0]` still starts with
   `x-anthropic-billing-header:`.

Then re-check the Accept-Encoding override, which rides a second undocumented internal:

4. Confirm `forward_headers_from_request()` still applies `x-pass-`-prefixed headers
   *after* merging the client's own, and that `accept-encoding` is still absent from its
   `_PASS_THROUGH_PROTECTED_HEADERS` set — if either changes, the override stops winning
   and non-streaming replies start failing to decode again. See
   [Why the upstream Accept-Encoding is pinned](#why-the-upstream-accept-encoding-is-pinned).

Then re-check request logging, which depends on LiteLLM internals in two separate places
and fails silently in both:

5. Confirm **successes** appear in `messages.jsonl`, not just failures. They reach the
   callback only via `litellm._async_success_callback` — see
   [Request logging is registered in three different places](#request-logging-is-registered-in-three-different-places).
   Checking that requests 200 is *not* sufficient: a past regression had every request
   succeeding while nothing but failures was logged.
6. Confirm **failures** still appear — on a **streaming** request, not just a
   non-streaming one. The two shapes reach the callback by different lists
   (`async_post_call_failure_hook` vs `async_log_failure_event`), and a past regression
   had non-streaming failures logging normally while every streaming failure was dropped.
7. Confirm there is exactly **one** record per request. `callback.py` is re-exec'd by
   LiteLLM's `get_instance_fn` (a fresh module, never cached in `sys.modules`), so a
   distinct `FileLogger` is registered per load; only `_add_custom_logger_to_list`'s
   class-name dedup plus `should_run_logging` keep that from double-logging. Duplicate
   records mean that assumption no longer holds.
