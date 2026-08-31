# This file has been edited with the assistance of an AI tool.
"""
Provider interface definition.

Every provider routes model traffic through a LiteLLM sidecar — that is a structural
invariant, not a convention. A provider is therefore a thin factory: it declares its
own proxy container plus its pricing table, and supplies the image pin, the auth-key
paths, the agent-side env vars, and its resolved on-disk paths.

The container is named after the provider, so agents on different providers run
concurrently against their own sidecars instead of fighting over one.

The container lifecycle — lazy start, health polling, network-mode detection, and
master key minting/recovery — lives in ``agent_wrap/domain/sidecars/litellm.py``.
Subclasses override a handful of class attributes and two abstract env hooks; the base
wires them into a ``LiteLLMSidecarConfig`` in ``sidecar()``.
"""

from abc import ABC, abstractmethod
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from agent_wrap.constants import (
    CONTAINER_NAME_PREFIX,
    LITELLM_IMAGE,
    LITELLM_LOGS_DIRNAME,
    SIDECAR_NETWORK_NAME,
    TOOL_DIR,
)
from agent_wrap.domain.providers.constants import (
    DEFAULT_SIDECAR_PORT,
    MODEL_CONTEXT_SUFFIX_RE,
    UNKNOWN_MODEL_COST_THRESHOLD_USD,
)
from agent_wrap.domain.providers.pricing import CostComputer, ModelKeyMatcher

if TYPE_CHECKING:
    from agent_wrap.domain.display.service import DisplayService
    from agent_wrap.domain.pricing.models import TokenUsage
    from agent_wrap.domain.providers.models import Tier
    from agent_wrap.domain.sidecars.base import Sidecar
    from agent_wrap.domain.sidecars.service import SidecarService


class Provider(ABC):
    """
    Abstract base class for model-routing providers.

    Each provider declares the LiteLLM proxy sidecar an agent run depends on — its own,
    named after the provider. The launcher ensures it before docker run, splices the
    connectivity flags it returns into the agent's docker run command, and releases it
    after the last agent on this provider exits.
    """

    #: Provider name matching the AGENT_PROVIDER env var (e.g. "litellm-bedrock").
    #: Must be a lowercase slug (``[a-z0-9-]+``): it becomes both the sidecar's
    #: container name (see ``container_name``) and the ``<provider>`` segment of the
    #: request-log path, which ``litellm_runtime/callback.py`` validates.
    name: str

    # ------------------------------------------------------------------
    # Class attributes (overridden by subclasses)
    # ------------------------------------------------------------------

    #: Pinned Docker image with tag + digest.
    image: ClassVar[str] = LITELLM_IMAGE
    #: Prefix for generated master keys (e.g. "sk-aw-" for bedrock).
    master_key_prefix: ClassVar[str] = "sk-aw-"
    #: Human-readable description of the API key this provider needs.
    #: Subclasses override this; the key name ``"api_key"`` is fixed. Left empty by
    #: a provider that needs no upstream secret at all (e.g. a local model).
    secret_description: ClassVar[str] = ""
    #: Whether the launcher injects CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 into the
    #: agent container for this provider. That flag also disables Claude Code's
    #: feature-flag evaluation against Anthropic's backend, which gates Remote Control
    #: and other Anthropic-backed features — a provider whose users need those to keep
    #: working overrides this to False.
    disable_nonessential_traffic: ClassVar[bool] = True
    #: Whether `agent run` starts the `agent logs` background viewer for this provider.
    #: That viewer is the only writer of the usage totals the bundled statusline reads,
    #: so leaving it down costs this provider's users their token/cost segment. A
    #: provider whose statusline segment is fed from somewhere else -- a subscription
    #: reports seat consumption, not spend -- overrides this to False.
    autostart_logs_viewer: ClassVar[bool] = True

    # ------------------------------------------------------------------
    # Shared defaults (rarely overridden)
    # ------------------------------------------------------------------

    network_name: ClassVar[str] = SIDECAR_NETWORK_NAME
    #: Preferred base port for this provider's sidecar. Not the port finally used: the
    #: sidecar scans upward from here at cold start and records what it resolved, so
    #: every provider can share one base without colliding in host-network mode.
    internal_port: ClassVar[int] = DEFAULT_SIDECAR_PORT
    health_timeout_sec: ClassVar[int] = 90
    health_endpoint: ClassVar[str] = "/health/liveliness"
    #: Seconds a cold start takes (docker run + health poll). The one launcher that
    #: wins the shared lock pays this; it dominates the lock-timeout budget. Kept
    #: above health_timeout_sec for the docker-run + reap tail.
    cold_start_time: ClassVar[float] = 120.0
    #: Seconds one agent takes to walk the lock on the hot path (sidecar already up:
    #: recover key + connectivity). Sub-second in practice; the runner multiplies it
    #: by the expected queue depth to size the lock timeout.
    short_circuit_time: ClassVar[float] = 2.0

    def __init__(
        self,
        sidecar_service: SidecarService,
        display_service: DisplayService,
    ) -> None:
        self._sidecar_service = sidecar_service
        self._display = display_service
        self._usage_convention_warned = False

    @classmethod
    def required_secrets(cls) -> list[tuple[str, str]]:
        """
        Return ``(key_name, description)`` tuples for secrets this provider needs.

        A provider needing no upstream secret leaves ``secret_description`` empty and
        gets an empty list; the resolved secrets dict reaching ``get_sidecar_env`` is
        keyed by exactly the names returned here.
        """
        if cls.secret_description:
            return [("api_key", cls.secret_description)]
        return []

    # ------------------------------------------------------------------
    # Sidecar declaration
    # ------------------------------------------------------------------

    @property
    def container_name(self) -> str:
        """
        Name this provider's own sidecar container: ``agent-wrap-<name>``.

        Per-provider rather than shared, so two agents on different providers each get
        their own upstream instead of one inheriting the other's. It is also the
        runner's refcount key, so each provider's sidecar is torn down independently.

        A subclass may still pin a literal by assigning ``container_name = "…"``: a
        class attribute shadows this property via the MRO.
        """
        return f"{CONTAINER_NAME_PREFIX}-{self.name}"

    def sidecar(self) -> Sidecar:
        """Return the LiteLLM proxy sidecar an agent run with this provider depends on."""
        return self._sidecar_service.create_litellm_sidecar(**self._sidecar_config())

    def _sidecar_config(self) -> dict[str, object]:
        """Build the sidecar config kwargs, closing over this provider's hooks and paths."""
        return {
            "image": self.image,
            "container_name": self.container_name,
            "network_name": self.network_name,
            "internal_port": self.internal_port,
            "master_key_prefix": self.master_key_prefix,
            "provider_name": self.name,
            "health_timeout_sec": self.health_timeout_sec,
            "health_endpoint": self.health_endpoint,
            "cold_start_time": float(self.cold_start_time),
            "short_circuit_time": float(self.short_circuit_time),
            "config_path": self._config_path(),
            "callback_dir": self._callback_dir(),
            "log_dir": self._log_dir(),
            "get_sidecar_env": self.get_sidecar_env,
            "get_agent_env": self.get_agent_env,
            "on_started": self.on_started,
            "on_stopping": self.on_stopping,
            "required_secrets": self.required_secrets(),
        }

    # ------------------------------------------------------------------
    # Abstract hooks (subclasses must implement)
    # ------------------------------------------------------------------

    @abstractmethod
    def get_sidecar_env(self, secrets: dict[str, Any]) -> dict[str, str]:
        """
        Return env vars for the sidecar container (upstream auth tokens).

        *secrets* is keyed by the names this provider declared in
        ``required_secrets()`` — empty when it declared none.
        """

    @abstractmethod
    def get_agent_env(self, master_key: str, base_url: str) -> dict[str, str]:
        """Return env vars injected into the agent container."""

    # ------------------------------------------------------------------
    # Optional lifecycle hooks (overridden by subclasses)
    # ------------------------------------------------------------------

    def on_started(self, master_key: str) -> None:  # noqa: B027
        """
        Run once, under the lock, right after the sidecar is started.

        Default no-op. Subclasses that must register the master key (e.g. approve
        it in .claude.json) override this — it runs exactly once per sidecar
        lifetime, not per agent.
        """

    def on_stopping(self, master_key: str) -> None:  # noqa: B027
        """
        Run once, under the lock, right before the sidecar is stopped.

        Default no-op. The inverse of on_started (e.g. un-approve the master key).
        """

    # ------------------------------------------------------------------
    # Config resolution (introspect the provider subclass module)
    # ------------------------------------------------------------------

    def _config_path(self) -> Path:
        """Resolve config.yaml next to this provider's provider.py."""
        return self._state_dir() / "config.yaml"

    def _state_dir(self) -> Path:
        """Resolve the provider's source directory (for lock/activity/state files)."""
        return (
            TOOL_DIR
            / "agent_wrap"
            / "domain"
            / "providers"
            / self.__class__.__module__.split(".")[-2]
        )

    def _callback_dir(self) -> Path:
        """Resolve the shared LiteLLM logging callback (mounted into the sidecar)."""
        return Path(__file__).parent / "litellm_runtime"

    def _log_dir(self) -> Path:
        """
        Shared host directory bind-mounted into the sidecar at /var/log/agent-wrap.

        Project-independent: a single directory under the agent-wrap install root.
        The callback writes to <project_hash>/<provider>/<session_id>/ beneath it,
        using the x-agent-wrap-log-prefix header the wrapper injects per launch and
        the AGENT_WRAP_PROVIDER env var set on the sidecar. This is required because
        each provider's sidecar (first-launch-wins per provider) serves every project
        on the host, and several providers' sidecars share this directory — the
        <provider> segment is what keeps their subtrees disjoint.
        """
        return TOOL_DIR / LITELLM_LOGS_DIRNAME

    # ------------------------------------------------------------------
    # Raw pricing data (subclass contract)
    # ------------------------------------------------------------------

    def _get_pricing(self, *, refresh_pricing_data: bool = False) -> dict[str, dict[str, float]]:
        """
        Return a flat pricing table for this provider.

        Keys are canonical model identifiers (e.g., 'claude-sonnet-4-5').
        Values are dicts with keys: 'in', 'out', 'cw_5m', 'cw_1h', 'cr'
        representing the cost per 1 million tokens.

        *refresh_pricing_data* re-fetches pricing from upstream, bypassing cached data.

        Raises ``NotImplementedError`` by default — providers that support
        flat-rate pricing must override.
        """
        raise NotImplementedError

    def _get_tiered_pricing(self, *, refresh_pricing_data: bool = False) -> dict[str, list[Tier]]:
        """
        Return a tiered pricing table for this provider.

        Keys are canonical model identifiers.  Values are lists of
        :class:`Tier` dicts, each with 'max_in' (token threshold), 'in_',
        'out', 'cw_5m', 'cw_1h', and 'cr' fields.

        *refresh_pricing_data* re-fetches pricing from upstream, bypassing cached data.

        Raises ``NotImplementedError`` by default — providers that support
        tiered pricing must override.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Pricing table construction
    # ------------------------------------------------------------------

    @cache  # noqa: B019
    def _build_pricing_table(self, *, refresh_pricing_data: bool = False) -> dict[str, list[Tier]]:
        """
        Build a unified tiered pricing table.

        Tries ``_get_tiered_pricing()`` first (already in the right shape).
        Falls back to ``_get_pricing()``, converting each flat-rate entry
        into a single infinite tier.  Returns an empty dict when neither
        method is implemented.

        *refresh_pricing_data* re-fetches pricing from upstream instead of serving any cached
        table; the result is still cached under its (self, refresh_pricing_data) key.
        """
        if refresh_pricing_data:
            self._build_pricing_table.cache_clear()
        try:
            return self._get_tiered_pricing(refresh_pricing_data=refresh_pricing_data)
        except NotImplementedError:
            pass

        try:
            flat = self._get_pricing(refresh_pricing_data=refresh_pricing_data)
        except NotImplementedError:
            return {}

        table: dict[str, list[Tier]] = {}
        for model_key, rates in flat.items():
            table[model_key] = [
                {
                    "max_in": float("inf"),
                    "in_": rates["in"],
                    "out": rates["out"],
                    "cw_5m": rates["cw_5m"],
                    "cw_1h": rates["cw_1h"],
                    "cr": rates["cr"],
                }
            ]
        return table

    def _cost_for_tiers(self, tiers: list[Tier], usage: TokenUsage) -> float:
        """
        Calculate the cost of a single request given its applicable tier list.

        *tiers* must be sorted by ``max_in`` (ascending). The first tier whose
        ``max_in >= input_tokens`` wins; the last tier is the fallback.
        """
        cost, convention_warn = CostComputer.cost_for_tiers(tiers, usage)
        if convention_warn and not self._usage_convention_warned:
            self._usage_convention_warned = True
            in_tokens: int = usage["input_tokens"]
            cc = usage.get("cache_creation", {})
            cw_5m: int = cc.get("ephemeral_5m_input_tokens", 0) or 0
            cw_1h: int = cc.get("ephemeral_1h_input_tokens", 0) or 0
            if not (cw_5m or cw_1h):
                cw_5m = usage.get("cache_creation_input_tokens", 0)
            cr_tokens: int = usage["cache_read_input_tokens"]
            self._display.warning(
                "token usage convention drift detected — "
                f"input_tokens ({in_tokens}) < cache-write ({cw_5m + cw_1h}) + "
                f"cache-read ({cr_tokens}). Cost math assumes input_tokens is "
                "inclusive of cache tokens; this record violates that. Reported "
                "costs may be inaccurate until "
                "agent_wrap/domain/providers/pricing.py:CostComputer.cost_for_tiers "
                "is revisited."
            )
        return cost

    def compute_cost(
        self,
        model: str,
        usage: TokenUsage,
        *,
        refresh_pricing_data: bool = False,
    ) -> float | None:
        """
        Compute the USD cost of a single request, or None if pricing is unknown.

        The default implementation builds the pricing table from
        ``_get_tiered_pricing()`` or ``_get_pricing()``, strips context-length
        suffixes from *model*, prefix-matches against pricing keys, selects the
        appropriate tier, and computes the cost.

        Subclasses can override this method to add custom logic (e.g., time-of-day
        multipliers).  *model* arrives already-normalized by ``PricingService``
        (Claude display names → canonical keys), but the default implementation
        still tolerates raw model names as a fallback.

        *refresh_pricing_data* re-fetches pricing from upstream instead of serving the cached
        pricing table.

        When *model* has no pricing-table match, this returns a known ``0.0``
        instead of ``None`` if the usage's cost would round down to $0 even
        under the most expensive tier this provider knows — see
        ``CostComputer.worst_case_cost``.
        """
        table = self._build_pricing_table(refresh_pricing_data=refresh_pricing_data)
        if not table:
            return None

        # Try candidates in order: as-received, then [1m]-stripped.
        seen: set[str] = set()
        unique: list[str] = []
        for c in (model, MODEL_CONTEXT_SUFFIX_RE.sub("", model)):
            if c and c not in seen:
                seen.add(c)
                unique.append(c)

        tiers = None
        for key in unique:
            match = ModelKeyMatcher.best_prefix_key(key, table)
            if match is not None:
                tiers = table[match]
                break

        if tiers is None:
            worst = CostComputer.worst_case_cost(table, usage)
            return 0.0 if worst < UNKNOWN_MODEL_COST_THRESHOLD_USD else None
        return self._cost_for_tiers(tiers, usage)
