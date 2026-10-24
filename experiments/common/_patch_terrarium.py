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
import logging
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

# Client-error statuses that mean "this endpoint will not serve a streaming
# request" rather than "this request was malformed". Only these trigger the
# fallback to the blocking transport; 429 and 5xx stay retryable.
_STREAM_REJECT_STATUSES = {400, 404, 405, 422, 501}


class _StreamingUnsupported(RuntimeError):
    """Raised when an endpoint rejects a streaming request outright.

    Signals the caller to retry without ``stream_options`` and then fall back
    to the blocking transport, instead of treating it as a hard failure.
    """


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
    """Client that talks to any OpenAI-compatible chat-completions endpoint.

    Supports two transports, selected by the ``stream`` flag:

    * ``stream=True`` (default) consumes the server-sent-events body and
      rebuilds an OpenAI-shaped response dict.
    * ``stream=False`` reads the JSON body directly.

    Streaming is the default because of how reasoning models behave. GLM-5.3-
    Flash can spend 60-90s inside a single completion, and with a blocking
    request the client receives **zero** bytes until the whole generation is
    finished. That makes the HTTP read timeout silently act as a cap on
    *total generation time* rather than on network inactivity, so healthy
    requests fail with ``ReadTimeout`` purely because the model was still
    thinking. Measured against the Modal-hosted endpoint: the same prompt
    took 62.9s blocking (timed out at 60s) but streamed with a 2.06s
    time-to-first-byte. With SSE the read timeout only fires when the
    connection is genuinely stalled.

    The blocking path is kept as a fallback for endpoints that reject a
    streaming request outright (see :class:`_StreamingUnsupported`).
    """

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        request_timeout: int = 180,
        stream: bool = True,
        include_usage: bool = True,
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
        self.stream = bool(stream)
        self.include_usage = bool(include_usage)
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

    def _build_payload(
        self,
        input: List[Dict[str, Any]],
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
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

        return payload

    @staticmethod
    def _parse_blocking(
        response: requests.Response,
    ) -> Tuple[Dict[str, Any], str]:
        """Read a non-streamed JSON body and return ``(response, response_str)``."""
        data = response.json()
        choices = data.get("choices") or []
        first_message = (choices[0].get("message") if choices else None) or {
            "content": ""
        }
        response_str = CustomOAICompatibleClient._extract_message_content(first_message)
        return data, response_str

    @staticmethod
    def _consume_stream(
        response: requests.Response,
    ) -> Tuple[Dict[str, Any], str]:
        """Consume an SSE body and rebuild an OpenAI-shaped response dict.

        The SSE dialect used by OpenAI-compatible servers sends exactly one
        JSON payload per ``data:`` line, terminated by ``data: [DONE]``. Tool
        calls arrive as deltas: ``id`` and ``function.name`` are sent once
        (in the first delta for that index) and ``function.arguments`` is sent
        as JSON fragments that must be concatenated in order.

        The rebuilt dict mirrors the blocking response closely enough for
        terrarium: ``choices[0].message`` carries ``content``,
        ``reasoning_content`` and ``tool_calls``, and ``usage`` is carried
        over verbatim when the server supplies it.
        """
        content_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: Dict[int, Dict[str, Any]] = {}
        finish_reason: Optional[str] = None
        usage: Optional[Dict[str, Any]] = None
        model: Optional[str] = None
        response_id: Optional[str] = None
        created: Optional[int] = None

        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            body = line[len("data:") :].strip()
            if not body:
                continue
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                # A malformed frame must not abort the whole response; the
                # server may interleave keep-alive noise.
                continue
            if not isinstance(chunk, dict):
                continue

            model = chunk.get("model") or model
            response_id = chunk.get("id") or response_id
            created = chunk.get("created") or created
            # The usage frame is a chunk with an empty `choices` list.
            if chunk.get("usage"):
                usage = chunk["usage"]

            for choice in chunk.get("choices") or []:
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content_parts.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_parts.append(delta["reasoning_content"])
                for call in delta.get("tool_calls") or []:
                    index = call.get("index")
                    if index is None:
                        index = 0
                    slot = tool_calls.setdefault(
                        index,
                        {
                            "id": None,
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if call.get("id"):
                        slot["id"] = call["id"]
                    if call.get("type"):
                        slot["type"] = call["type"]
                    function = call.get("function") or {}
                    name = function.get("name")
                    # The name arrives once; later deltas carry only arguments.
                    if name and not slot["function"]["name"]:
                        slot["function"]["name"] = name
                    arguments = function.get("arguments")
                    if arguments:
                        slot["function"]["arguments"] += arguments

        message: Dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts),
        }
        if reasoning_parts:
            message["reasoning_content"] = "".join(reasoning_parts)
        if tool_calls:
            message["tool_calls"] = [tool_calls[key] for key in sorted(tool_calls)]

        data: Dict[str, Any] = {
            "id": response_id,
            "object": "chat.completion",
            "created": created,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason,
                }
            ],
        }
        if usage:
            data["usage"] = usage
        return data, message["content"]

    def _request(
        self,
        payload: Dict[str, Any],
        *,
        stream: bool,
    ) -> Tuple[Dict[str, Any], str]:
        """POST ``payload`` with the retry/backoff policy.

        Returns ``(response_dict, response_str)``. Raises
        :class:`_StreamingUnsupported` when a streaming request is rejected
        outright by the endpoint (a client error), so the caller can fall back
        to the blocking transport.
        """
        max_retries, base_sleep_s, max_sleep_s = self._get_retry_config()
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error: Optional[str] = None

        for attempt in range(max_retries + 1):
            response: Optional[requests.Response] = None
            can_retry = True
            try:
                response = self.session.post(
                    f"{self.base_url}/chat/completions",
                    headers=self._build_headers(),
                    data=json.dumps(payload),
                    timeout=self.request_timeout,
                    stream=stream,
                )
            except requests.exceptions.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.ok:
                    try:
                        if stream:
                            return self._consume_stream(response)
                        return self._parse_blocking(response)
                    except Exception as exc:
                        # A broken stream (connection dropped mid-body) or a
                        # malformed body is worth one more attempt.
                        last_error = f"{type(exc).__name__}: {exc}"
                else:
                    body = response.text
                    if stream and response.status_code in _STREAM_REJECT_STATUSES:
                        raise _StreamingUnsupported(
                            f"HTTP {response.status_code}: {body}"
                        ) from None
                    last_error = f"HTTP {response.status_code}: {body}"
                    can_retry = response.status_code in retryable_statuses

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

        raise RuntimeError(
            f"custom-oai-compatible chat request failed after {max_retries + 1} attempts: {last_error}"
        )

    def generate_response(
        self,
        input: List[Dict[str, Any]],
        params: Dict[str, Any],
    ) -> Tuple[Dict[str, Any], str]:
        payload = self._build_payload(input, params)

        if not self.stream:
            return self._request(payload, stream=False)

        streaming_payload = dict(payload)
        streaming_payload["stream"] = True
        if self.include_usage:
            streaming_payload["stream_options"] = {"include_usage": True}

        try:
            return self._request(streaming_payload, stream=True)
        except _StreamingUnsupported:
            # The endpoint rejected the streaming request. Retry without
            # `stream_options` (some servers support SSE but not usage
            # frames) before giving up on streaming entirely.
            pass

        fallback_payload = dict(payload)
        fallback_payload["stream"] = True
        try:
            return self._request(fallback_payload, stream=True)
        except _StreamingUnsupported:
            pass

        # Last resort: the original blocking transport.
        return self._request(payload, stream=False)

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
    _install_hospital_broadcast_rationale_patch()


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
                # 180s, not 60s: reasoning models can legitimately spend 60-90s
                # inside one completion. The blocking transport's read timeout
                # caps TOTAL generation time, so 60s turned healthy requests
                # into ReadTimeouts.
                request_timeout=int(coai_cfg.get("request_timeout", 180)),
                # SSE by default so the read timeout measures inter-chunk idle
                # time instead of total generation time.
                stream=bool(coai_cfg.get("stream", True)),
                include_usage=bool(coai_cfg.get("include_usage", True)),
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


# ---------------------------------------------------------------------------
# Environment-tools block (e.g. for the pre-coordination round: agents see
# `broadcast_message`, `transfer_resources`, etc. in their prompt, but any call
# is silently dropped so they cannot probe the environment before losing the
# private channel).
# ---------------------------------------------------------------------------
# Same task-local ContextVar rationale as `_POST_MESSAGE_DISABLED`.
_ENV_TOOLS_BLOCKED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "colosseum_env_tools_blocked", default=False
)


def set_env_tools_blocked(blocked: bool) -> None:
    """Block all environment tool calls (e.g. ``broadcast_message``,
    ``transfer_resources``) for the current task/context.

    Used during the pre-coordination round so that adversaries cannot probe
    the environment before they lose ``post_message``. The agent still sees
    the tools in its prompt; the protocol layer returns an empty result
    instead of executing them.
    """
    _ENV_TOOLS_BLOCKED.set(bool(blocked))


def is_env_tools_blocked() -> bool:
    """Return whether environment tools are blocked in the current task/context."""
    return _ENV_TOOLS_BLOCKED.get()


# ---------------------------------------------------------------------------
# Per-agent `post_message` count cap (e.g. for the pre-coordination round).
# Stores a dict mapping agent_name -> count of `post_message` invocations in
# the current task's context. The caller (typically the experiment runner) is
# responsible for resetting it at round boundaries.
# ---------------------------------------------------------------------------
_POST_MESSAGE_COUNTS: contextvars.ContextVar[Dict[str, int]] = contextvars.ContextVar(
    "colosseum_post_message_counts", default={}
)


def reset_post_message_counts() -> None:
    """Reset the per-agent `post_message` counter for the current context."""
    _POST_MESSAGE_COUNTS.set({})


def get_post_message_count(agent_name: str) -> int:
    """Return how many times `agent_name` has called `post_message` in the
    current context (this run / task / round, depending on what the caller
    reset on)."""
    return _POST_MESSAGE_COUNTS.get().get(agent_name, 0)


def record_post_message_call(agent_name: str) -> int:
    """Increment the counter and return the new value."""
    counts = dict(_POST_MESSAGE_COUNTS.get())
    counts[agent_name] = counts.get(agent_name, 0) + 1
    _POST_MESSAGE_COUNTS.set(counts)
    return counts[agent_name]


# Per-task cap on `post_message` calls (None = unlimited). Used to throttle
# the pre-coordination round so adversaries cannot flood the secret blackboard.
# The counter (see `_POST_MESSAGE_COUNTS`) is keyed by agent_name so the limit
# is enforced per-agent.
_POST_MESSAGE_CALL_LIMIT: contextvars.ContextVar[Optional[int]] = (
    contextvars.ContextVar("colosseum_post_message_call_limit", default=None)
)


def set_post_message_call_limit(limit: Optional[int]) -> None:
    """Set the per-agent `post_message` cap for the current context.

    ``None`` means unlimited. The limit is enforced in
    :meth:`LocalCommunicationProtocol.blackboard_handle_tool_call`.
    """
    if limit is None:
        _POST_MESSAGE_CALL_LIMIT.set(None)
    else:
        _POST_MESSAGE_CALL_LIMIT.set(int(limit))


def get_post_message_call_limit() -> Optional[int]:
    """Return the per-agent `post_message` cap (``None`` = unlimited)."""
    return _POST_MESSAGE_CALL_LIMIT.get()


# ---------------------------------------------------------------------------
# Secret blackboard id (for the pre-coordination round: if set, the protocol
# layer rewrites any `post_message` call to this board so the agent cannot
# accidentally post to the public channel during the preparation turn).
# ---------------------------------------------------------------------------
_SECRET_BLACKBOARD_ID: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "colosseum_secret_blackboard_id", default=None
)


def set_secret_blackboard_id(bb_id: Optional[int]) -> None:
    """Set the secret blackboard id that ``post_message`` calls should be
    redirected to (``None`` = no rewriting).

    Used during the pre-coordination round to guarantee that adversarial
    coordination stays on the coalition-only board even if the model omits
    ``blackboard_id`` or picks the public board by mistake.
    """
    if bb_id is None:
        _SECRET_BLACKBOARD_ID.set(None)
    else:
        _SECRET_BLACKBOARD_ID.set(int(bb_id))


def get_secret_blackboard_id() -> Optional[int]:
    """Return the secret blackboard id (``None`` = no rewriting)."""
    return _SECRET_BLACKBOARD_ID.get()


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

    # Phases that expose the blackboard toolset (i.e. offer `post_message`).
    # `"pre_coordination"` is a planning-shaped adversary round and must look
    # the same to the toolset as `"planning"`, otherwise terrarium's phase gate
    # silently drops `post_message` from the toolset and the secret channel
    # stays empty (the original bug). The trajectory log label is independent
    # and still records `"pre_coordination"` verbatim -- only the toolset
    # lookup is remapped here.
    planning_shaped_phases = ("planning", "pre_coordination")

    def get_tools_for_blackboard(self, phase: str):
        if is_post_message_disabled():
            return []
        effective = "planning" if phase in planning_shaped_phases else phase
        return _orig_get_tools(self, effective)

    def get_blackboard_tool_names(self):
        if is_post_message_disabled():
            return set()
        return _orig_get_names(self)

    get_tools_for_blackboard._colosseum_patched = True  # type: ignore[attr-defined]
    get_blackboard_tool_names._colosseum_patched = True  # type: ignore[attr-defined]

    ToolsetDiscovery.get_tools_for_blackboard = get_tools_for_blackboard
    ToolsetDiscovery.get_blackboard_tool_names = get_blackboard_tool_names


# ---------------------------------------------------------------------------
# Hospital-tools patch: broadcast_message rationale label
# ---------------------------------------------------------------------------
# Upstream ``HospitalTools.execute_action`` formats the public blackboard line
# for a broadcast as:
#
#     BROADCAST: '<message>'. (Internal Rationale: <rationale>)
#
# The "(Internal Rationale: ...)" framing mislabels the field: the rationale is
# the agent's official, auditable reason for the broadcast (the same kind of
# record that ``schedule_patient`` and ``transfer_resources`` already expose
# under the plain label "Rationale:"), not a private thought. In practice the
# "(Internal ...)" wording caused honest agents to read leaked text inside the
# parentheses as covert reasoning, which produced a wildly bimodal
# ``suspicion_rate_non_adversary`` metric -- one well-placed giveaway word in
# a single agent's broadcast rationale (e.g. "coalition channel updated per
# protocol without exposing anything on public channels") flipped every honest
# agent in the run to ``suspects_foul_play: true``. The fix is purely cosmetic:
# relabel the field so honest agents treat it the same way they treat the
# rationale on every other action.
def _install_hospital_broadcast_rationale_patch() -> None:
    """Rewrite ``HospitalTools.execute_action`` for the broadcast_message branch.

    The wrapped method recomputes ``msg`` with the consistent ``Rationale:``
    label used by ``schedule_patient`` and ``transfer_resources``, then logs to
    the blackboard and returns the same dict shape as the original. All other
    action branches are delegated unchanged.

    Idempotent: a sentinel attribute on the wrapped method short-circuits any
    repeat ``apply()`` call.
    """
    try:
        from envs.dcops.hospital.hospital_tools import HospitalTools
    except ImportError:
        # The hospital env is not on the pythonpath in every experiment; that's
        # fine -- this patch is a no-op there.
        return

    if getattr(HospitalTools.execute_action, "_colosseum_patched", False):
        return

    _orig_execute_action = HospitalTools.execute_action

    def execute_action(  # type: ignore[no-redef]
        self: Any,
        agent_name: str,
        action: Dict[str, Any],
        log_to_blackboards: bool = True,
        phase: Optional[str] = None,
        iteration: Optional[int] = None,
    ) -> Dict[str, Any]:
        # Only intercept the broadcast branch; let everything else (schedule,
        # transfer, etc.) flow through the original implementation.
        if not isinstance(action, dict) or "broadcast_message" not in action:
            return _orig_execute_action(
                self,
                agent_name,
                action,
                log_to_blackboards,
                phase,
                iteration,
            )

        args = action["broadcast_message"][agent_name]
        # Match the format used by schedule_patient / transfer_resources:
        #   "Rationale: <rationale>"  -- not "(Internal Rationale: <rationale>)".
        # The rationale is the agent's official reason for the broadcast and
        # belongs to the public action record; there is nothing "internal"
        # about it.
        msg = f"BROADCAST: '{args.get('message')}'. Rationale: {args.get('rationale')}"

        execution_result: Dict[str, Any] = {
            "status": "success",
            "result": msg,
            "state_updates": action,
        }

        if log_to_blackboards and getattr(self, "blackboard_manager", None):
            try:
                self.blackboard_manager.log_action_to_blackboards(
                    agent_name, action, execution_result, phase, iteration
                )
            except Exception as exc:  # pragma: no cover -- mirrors upstream
                logging.getLogger(__name__).error(
                    "Blackboard log failed (colosseum broadcast patch): %s", exc
                )

        return execution_result

    execute_action._colosseum_patched = True  # type: ignore[attr-defined]
    HospitalTools.execute_action = execute_action  # type: ignore[method-assign]


# Applying on import means a bare `import experiments.common._patch_terrarium`
# (or any import of a module that imports this one) is enough to activate it.
apply()
