"""Runtime monkey-patch that teaches terrarium about the ``custom_oai_compatible`` provider.

This module is colosseum's own implementation of the ``custom_oai_compatible``
LLM provider slot. It exists because the upstream ``terrarium-agents`` package
ships a fixed provider list (``openai, anthropic, gemini, together, fireworks,
vllm``) and the provider slot we need is not part of it.

The snake_case spelling ``custom_oai_compatible`` is the ONLY accepted provider
string. The hyphenated spelling ``custom-oai-compatible`` used to be accepted too,
but it silently dropped the provider's ``params``: terrarium keys its lookups on
``llm_config.get(provider.lower())`` (see ``terrarium.utils.get_generation_params``),
so ``provider: custom-oai-compatible`` looked up the nonexistent
``custom-oai-compatible`` YAML block and returned ``{}``. The hyphenated spelling
now raises a clear :class:`ValueError` (via :func:`normalize_provider_name`) rather
than failing silently.

Rather than fork terrarium — or edit files inside ``.venv/`` (which are wiped by
``uv sync`` and absent on a fresh checkout) — we patch the dispatch functions at
runtime, from a file that *is* committed to this repo:

* ``terrarium.utils.get_client_instance`` — returns a
  :class:`CustomOAICompatibleClient` for ``custom_oai_compatible`` and delegates
  everything else to the original.
* ``terrarium.utils.get_model_name`` — same treatment for model-name lookup.
* ``terrarium.utils.get_generation_params`` — resolves the ``custom_oai_compatible``
  ``params`` block for this provider (defense in depth; the upstream implementation
  would otherwise return ``{}`` for any non-snake_case alias).

Importing this module applies the patch (idempotently). Every runner imports
``experiments.common.run_utils`` early, which in turn imports this module, so
the patch is active long before any client is constructed. The standalone judge
CLIs import it directly too.

The client itself :class:`CustomOAICompatibleClient` mirrors terrarium's
``FireworksClient`` byte-for-byte apart from ``__init__`` (it resolves the base
URL / API key from ``OAI_COMPATIBLE_BASE_URL`` / ``OAI_COMPATIBLE_PROVIDER_KEY``
instead of Fireworks-specific settings).

This module also installs the blackboard-tools patch that lets a run suppress
the ``post_message`` tool. That toggle is **task-local**: it is stored in a
:class:`contextvars.ContextVar`, so the concurrent ``_run_single`` coroutines
scheduled by a sweep (``max_concurrent_runs > 1``) each see their own value and
can never clobber one another's restriction mid-round.
"""

from __future__ import annotations

import contextvars
import json
import os
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv

from llm_server.clients.abstract_client import AbstractClient
from llm_server.clients.fireworks_client import _convert_tools

# ``custom_oai_compatible`` (snake_case) is the only accepted spelling. The tuple
# form is kept for call-site readability, but it now holds exactly one canonical
# entry; the hyphenated spelling is rejected by ``normalize_provider_name`` below.
_CUSTOM_PROVIDER_ALIASES = ("custom_oai_compatible",)

# The legacy hyphenated spelling, kept only so we can detect and reject it loudly.
_HYPHENATED_CUSTOM_PROVIDER = "custom-oai-compatible"


def normalize_provider_name(provider: Any) -> str:
    """Normalize (and validate) a provider string.

    Returns the lowercased/stripped provider name. Raises :class:`ValueError` for
    the legacy hyphenated ``custom-oai-compatible`` spelling, which terrarium keys
    its config lookups on verbatim and therefore resolves to an empty ``params``
    dict (a silent failure). Any other provider string is returned unchanged
    (modulo case-folding / surrounding whitespace).
    """
    normalized = str(provider).strip().lower()
    if normalized == _HYPHENATED_CUSTOM_PROVIDER:
        raise ValueError(
            "provider 'custom-oai-compatible' is no longer supported: terrarium looks "
            "up its per-provider config block using the provider string verbatim, so the "
            "hyphenated spelling silently drops `params` (max_tokens, temperature, ...). "
            "Use the snake_case spelling instead:\n"
            "    provider: custom_oai_compatible"
        )
    return normalized


class CustomOAICompatibleClient(AbstractClient):
    """Client that talks to any OpenAI-compatible chat-completions endpoint."""

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        request_timeout: int = 60,
    ):
        load_dotenv(override=True)
        resolved_base_url = base_url or os.getenv("OAI_COMPATIBLE_BASE_URL")
        resolved_key = api_key or os.getenv("OAI_COMPATIBLE_PROVIDER_KEY")
        if not resolved_base_url or not resolved_key:
            raise ValueError(
                "Both OAI_COMPATIBLE_BASE_URL and OAI_COMPATIBLE_PROVIDER_KEY must be set "
                "(either via the YAML config or environment variables)."
            )

        self.base_url = str(resolved_base_url).rstrip("/")
        self.api_key = resolved_key
        self.request_timeout = int(request_timeout)
        self.session = requests.Session()

    @staticmethod
    def _get_retry_config() -> tuple[int, float, float]:
        max_retries = int(os.getenv("FIREWORKS_MAX_RETRIES", "6"))
        base_sleep_s = float(os.getenv("FIREWORKS_RETRY_BASE_SLEEP_S", "1.0"))
        max_sleep_s = float(os.getenv("FIREWORKS_RETRY_MAX_SLEEP_S", "30.0"))
        return max_retries, base_sleep_s, max_sleep_s

    @staticmethod
    def _get_retry_after_seconds(response: requests.Response) -> Optional[float]:
        retry_after = response.headers.get("Retry-After")
        if not retry_after:
            return None
        try:
            return float(retry_after)
        except ValueError:
            return None

    @staticmethod
    def init_context(system_prompt: str, user_prompt: str) -> List[Dict[str, Any]]:
        """Initialize chat-style context for the endpoint."""
        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

    @staticmethod
    def _extract_message_content(message: Dict[str, Any]) -> str:
        if isinstance(message, dict) and "choices" in message:
            choices = message.get("choices") or []
            message = choices[0].get("message") if choices else {}
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(item.get("text", ""))
            return "".join(parts)
        return ""

    @staticmethod
    def get_usage(
        response: Dict[str, Any], current_usage: Dict[str, int]
    ) -> Dict[str, int]:
        usage = response.get("usage") or {}
        current_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
        current_usage["completion_tokens"] += usage.get("completion_tokens", 0)
        if "total_tokens" in usage:
            current_usage["total_tokens"] += usage.get("total_tokens", 0)
        else:
            current_usage["total_tokens"] = (
                current_usage["prompt_tokens"] + current_usage["completion_tokens"]
            )
        return current_usage

    def _build_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def generate_response(
        self,
        input: List[Dict[str, Any]],
        params: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str]:
        payload: Dict[str, Any] = {
            "model": params.get("model"),
            "messages": input,
        }

        max_tokens = params.get("max_completion_tokens") or params.get(
            "max_output_tokens"
        )
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if params.get("temperature") is not None:
            payload["temperature"] = params["temperature"]

        converted_tools = _convert_tools(params.get("tools"))
        if converted_tools:
            payload["tools"] = converted_tools

        max_retries, base_sleep_s, max_sleep_s = self._get_retry_config()
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error: Optional[str] = None
        response: Optional[requests.Response] = None

        for attempt in range(max_retries + 1):
            try:
                response = self.session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._build_headers(),
                    data=json.dumps(payload),
                    timeout=self.request_timeout,
                )
            except requests.exceptions.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                response = None
            else:
                if response.ok:
                    last_error = None
                    break
                last_error = f"HTTP {response.status_code}: {response.text}"

            status_code = response.status_code if response is not None else None
            can_retry = (status_code in retryable_statuses) or (response is None)
            if attempt >= max_retries or not can_retry:
                break

            retry_after_s = (
                self._get_retry_after_seconds(response)
                if response is not None
                else None
            )
            if retry_after_s is not None and retry_after_s > 0:
                sleep_s = min(retry_after_s, max_sleep_s)
            else:
                exp_sleep = base_sleep_s * (2**attempt)
                jitter = random.uniform(0.0, min(1.0, exp_sleep * 0.25))
                sleep_s = min(max_sleep_s, exp_sleep + jitter)
            time.sleep(sleep_s)

        if last_error is not None:
            raise RuntimeError(
                f"custom-oai-compatible chat request failed after {max_retries + 1} attempts: {last_error}"
            )

        data = response.json()  # type: ignore[union-attr]
        choices = data.get("choices") or []
        first_message = choices[0]["message"] if choices else {"content": ""}
        response_str = self._extract_message_content(first_message)
        return data, response_str

    async def process_tool_calls(
        self,
        response: Dict[str, Any],
        context: List[Dict[str, Any]],
        execute_tool_callback: Any,
    ) -> Tuple[int, List[Dict[str, Any]], List[str]]:
        choices = response.get("choices") or []
        if not choices:
            return 0, context, []

        message = choices[0].get("message") or {}
        context.append(message)
        tool_calls = message.get("tool_calls") or []
        tool_calls_executed = 0
        step_tools: List[str] = []

        for call in tool_calls:
            function_block = call.get("function") or {}
            tool_name = function_block.get("name", "unknown_tool")
            arguments_raw = function_block.get("arguments") or "{}"
            try:
                arguments = json.loads(arguments_raw)
            except json.JSONDecodeError:
                arguments = {}
            step_tools.append(f"{tool_name} -- {json.dumps(arguments)}")
            result = await execute_tool_callback(tool_name, arguments)
            context.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": tool_name,
                    "content": json.dumps(result),
                }
            )
            tool_calls_executed += 1

        return tool_calls_executed, context, step_tools


def apply() -> None:
    """Install the `custom_oai_compatible` provider into terrarium's dispatch.

    Idempotent: repeated calls (or repeated imports) are a no-op. Each patch is
    guarded independently so a partially-applied state still converges.
    """
    import terrarium
    import terrarium.utils as _tu

    if not getattr(_tu.get_client_instance, "_colosseum_patched", False):
        _install_client_and_model_patch(terrarium, _tu)

    if not getattr(_tu.get_generation_params, "_colosseum_patched", False):
        _install_generation_params_patch(_tu)

    _install_blackboard_tools_patch()


def _install_client_and_model_patch(terrarium: Any, _tu: Any) -> None:
    """Patch ``get_client_instance`` / ``get_model_name`` (idempotently)."""
    _orig_get_client = _tu.get_client_instance
    _orig_get_model = _tu.get_model_name

    def get_client_instance(llm_config, *, agent_name=None, vllm_runtime=None):
        provider = normalize_provider_name(llm_config.get("provider") or "vllm")
        if provider in _CUSTOM_PROVIDER_ALIASES:
            coai_cfg = llm_config.get("custom_oai_compatible") or {}
            return CustomOAICompatibleClient(
                base_url=coai_cfg.get("base_url"),
                api_key=coai_cfg.get("api_key"),
                request_timeout=int(coai_cfg.get("request_timeout", 60)),
            )
        return _orig_get_client(
            llm_config, agent_name=agent_name, vllm_runtime=vllm_runtime
        )

    def get_model_name(provider, llm_config):
        normalized = (
            normalize_provider_name(provider) if isinstance(provider, str) else ""
        )
        if normalized in _CUSTOM_PROVIDER_ALIASES:
            return llm_config.get("custom_oai_compatible", {}).get(
                "model", "unknown-custom-oai-compatible-model"
            )
        return _orig_get_model(provider, llm_config)

    get_client_instance._colosseum_patched = True  # type: ignore[attr-defined]
    get_model_name._colosseum_patched = True  # type: ignore[attr-defined]

    _tu.get_client_instance = get_client_instance
    _tu.get_model_name = get_model_name

    # Some code may import these straight off the `terrarium` package.
    if getattr(terrarium, "get_client_instance", None) is _orig_get_client:
        terrarium.get_client_instance = get_client_instance
    if getattr(terrarium, "get_model_name", None) is _orig_get_model:
        terrarium.get_model_name = get_model_name


def _install_generation_params_patch(_tu: Any) -> None:
    """Patch ``get_generation_params`` so it resolves our provider's params block.

    Upstream keys the per-provider block on the provider string verbatim, which
    already works for the canonical ``custom_oai_compatible`` spelling; this patch
    is defense in depth and makes the rejection of the hyphenated spelling happen
    in one place (:func:`normalize_provider_name`). All other providers are
    delegated to the original implementation unchanged.
    """
    _orig_get_generation_params = _tu.get_generation_params

    def get_generation_params(llm_config: Dict[str, Any]) -> Dict[str, Any]:
        raw_provider = llm_config.get("provider")
        provider = normalize_provider_name(raw_provider) if raw_provider else ""
        if provider not in _CUSTOM_PROVIDER_ALIASES:
            return _orig_get_generation_params(llm_config)

        provider_block = llm_config.get("custom_oai_compatible", {})
        if not isinstance(provider_block, dict):
            return {}
        raw_params = (
            provider_block.get("params") or provider_block.get("generation") or {}
        )
        if not isinstance(raw_params, dict):
            return {}
        # Never leak model identifiers into request params (matches upstream).
        return {k: v for k, v in raw_params.items() if k != "model"}

    get_generation_params._colosseum_patched = True  # type: ignore[attr-defined]
    _tu.get_generation_params = get_generation_params


# ---------------------------------------------------------------------------
# Blackboard tools patch (e.g. disabling post_message for tool-based collusion)
# ---------------------------------------------------------------------------
# The flag is task-local, not process-global: a plain module-level bool would be
# clobbered whenever two `_run_single` coroutines overlap under
# `max_concurrent_runs > 1` (the last writer wins for every other in-flight run's
# remaining turns). A ContextVar gives each asyncio task its own copy; child
# tasks (and `copy_context().run(...)` at any thread boundary) inherit a snapshot.
_POST_MESSAGE_DISABLED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "colosseum_post_message_disabled", default=False
)


def set_post_message_disabled(disabled: bool) -> None:
    """Enable or disable the post_message tool for the current task/context.

    The value is scoped to the calling task's :mod:`contextvars` context, so
    concurrent runs cannot interfere with each other.
    """
    _POST_MESSAGE_DISABLED.set(bool(disabled))


def is_post_message_disabled() -> bool:
    """Return whether post_message is disabled in the current task/context."""
    return _POST_MESSAGE_DISABLED.get()


def _install_blackboard_tools_patch() -> None:
    """Patch ToolsetDiscovery to allow disabling post_message dynamically."""
    try:
        from terrarium.toolset_discovery import ToolsetDiscovery
    except ImportError:
        return

    if getattr(ToolsetDiscovery.get_tools_for_blackboard, "_colosseum_patched", False):
        return

    _orig_get_tools = ToolsetDiscovery.get_tools_for_blackboard
    _orig_get_names = ToolsetDiscovery.get_blackboard_tool_names

    def get_tools_for_blackboard(self, phase: str):
        if is_post_message_disabled():
            return []
        return _orig_get_tools(self, phase)

    def get_blackboard_tool_names(self):
        if is_post_message_disabled():
            return set()
        return _orig_get_names(self)

    get_tools_for_blackboard._colosseum_patched = True  # type: ignore[attr-defined]
    get_blackboard_tool_names._colosseum_patched = True  # type: ignore[attr-defined]

    ToolsetDiscovery.get_tools_for_blackboard = get_tools_for_blackboard
    ToolsetDiscovery.get_blackboard_tool_names = get_blackboard_tool_names


# Applying on import means a bare `import experiments.common._patch_terrarium`
# (or any import of a module that imports this one) is enough to activate it.
apply()
