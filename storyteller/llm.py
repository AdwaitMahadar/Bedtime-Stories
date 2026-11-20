"""The only module that talks to the Anthropic API.

- `load_api_key` / `check_api_key`: the key comes only from the environment or `.env`, and is
  verified at startup with a free model lookup. It is never stored or logged.
- `LLM.structured`: one call that must return a valid instance of a Pydantic model. It handles
  refusals, cut-off responses and malformed output, and records a trace event per attempt.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional, TypeVar

import anthropic
from anthropic import transform_schema
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from . import config
from .models import TraceEvent

T = TypeVar("T", bound=BaseModel)

KeyProblemKind = Literal["missing", "invalid", "no_access", "rate_limited", "network", "unknown"]

FIX_HINT = "Copy `.env.example` to `.env`, paste your key after `ANTHROPIC_API_KEY=`, then retry."


# --- Errors the UI and CLI can show directly ------------------------------------------------


class LLMError(Exception):
    """Base class: `str(error)` is a friendly, user-facing message."""


class APICallError(LLMError):
    def __init__(self, kind: KeyProblemKind, message: str):
        super().__init__(message)
        self.kind = kind


class RefusalError(LLMError):
    pass


class TruncatedError(LLMError):
    pass


class OutputFormatError(LLMError):
    pass


def describe_api_error(error: Exception) -> tuple[KeyProblemKind, str]:
    """Map an SDK exception to a problem kind and a message a parent or reviewer can act on."""
    if isinstance(error, anthropic.AuthenticationError):
        return "invalid", f"The API key was rejected. Check it for typos. {FIX_HINT}"
    if isinstance(error, (anthropic.PermissionDeniedError, anthropic.NotFoundError)):
        return "no_access", f"The API key works, but it doesn't have access to `{config.MODEL}`."
    if isinstance(error, anthropic.RateLimitError):
        return "rate_limited", "The API key is valid but rate-limited right now. Try again in a moment."
    if isinstance(error, anthropic.APIConnectionError):  # includes APITimeoutError
        return "network", "Couldn't reach Anthropic. Check your internet connection and retry."
    if isinstance(error, anthropic.APIStatusError):
        request_id = getattr(error, "request_id", None) or "n/a"
        return "unknown", f"Anthropic returned an error ({error.status_code}, request {request_id}). Please retry."
    return "unknown", f"Unexpected error: {error}"


# --- API key ---------------------------------------------------------------------------------


def load_api_key() -> Optional[str]:
    """Read ANTHROPIC_API_KEY from the environment, falling back to the repo's `.env`."""
    load_dotenv(config.REPO_ROOT / ".env")
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    return key or None


@dataclass
class KeyStatus:
    ok: bool
    kind: Optional[KeyProblemKind] = None
    message: str = ""


def check_api_key(api_key: Optional[str]) -> KeyStatus:
    """Verify the key with a free models lookup, so problems surface at startup, not mid-story."""
    if not api_key:
        return KeyStatus(False, "missing", f"No API key found. {FIX_HINT}")
    client = anthropic.Anthropic(api_key=api_key, max_retries=0, timeout=config.KEY_CHECK_TIMEOUT_S)
    try:
        client.models.retrieve(config.MODEL)
    except Exception as error:  # noqa: BLE001 - every failure becomes a readable status
        kind, message = describe_api_error(error)
        return KeyStatus(False, kind, message)
    return KeyStatus(True)


# --- Structured calls ------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _schema_for(model_cls: type[BaseModel]) -> dict:
    # The SDK helper moves constraints the API doesn't support (min/max, lengths) into field
    # descriptions; Pydantic still enforces them when we validate the response below.
    return transform_schema(model_cls)


def _cost(input_tokens: int, output_tokens: int) -> float:
    return (
        input_tokens * config.PRICE_INPUT_PER_MTOK + output_tokens * config.PRICE_OUTPUT_PER_MTOK
    ) / 1_000_000


class LLM:
    def __init__(self, api_key: str, model: str = config.MODEL):
        self.model = model
        self._client = anthropic.Anthropic(
            api_key=api_key, timeout=config.REQUEST_TIMEOUT_S, max_retries=config.SDK_MAX_RETRIES
        )

    def structured(
        self,
        *,
        step: str,
        system: str,
        user: str,
        schema: type[T],
        trace: list[TraceEvent],
    ) -> T:
        """Call the model and return a validated `schema` instance.

        Up to two attempts: a cut-off response is retried with double the token budget, and
        output that fails validation is retried once. Refusals are not retried.
        """
        max_tokens = config.MAX_TOKENS[step]
        failure = ""  # why the last attempt didn't produce a valid result
        for attempt in (1, 2):
            started = time.monotonic()
            try:
                response = self._client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    output_config={
                        "effort": config.EFFORT[step],
                        "format": {"type": "json_schema", "schema": _schema_for(schema)},
                    },
                )
            except Exception as error:  # noqa: BLE001
                kind, message = describe_api_error(error)
                trace.append(self._event(step, attempt, started, ok=False, error=kind))
                raise APICallError(kind, message) from error

            usage = response.usage
            event = self._event(
                step,
                attempt,
                started,
                ok=response.stop_reason == "end_turn",
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                stop_reason=response.stop_reason,
            )

            if response.stop_reason == "refusal":
                trace.append(event.model_copy(update={"error": "refusal"}))
                raise RefusalError(
                    "The model declined this request. Try rephrasing it as a gentle bedtime story idea."
                )
            if response.stop_reason == "max_tokens":
                trace.append(event.model_copy(update={"error": "max_tokens"}))
                failure = "truncated"
                max_tokens *= 2
                continue

            text = "".join(block.text for block in response.content if block.type == "text")
            try:
                parsed = schema.model_validate_json(text)
            except ValidationError as error:
                trace.append(event.model_copy(update={"ok": False, "error": "invalid_output"}))
                failure = f"the output didn't match the expected format ({error.error_count()} errors)"
                continue
            trace.append(event)
            return parsed

        if failure == "truncated":
            raise TruncatedError(f"The {step} step ran out of room twice. Please retry.")
        raise OutputFormatError(f"The {step} step failed twice: {failure}. Please retry.")

    def _event(self, step: str, attempt: int, started: float, **fields) -> TraceEvent:
        input_tokens = fields.get("input_tokens", 0)
        output_tokens = fields.get("output_tokens", 0)
        return TraceEvent(
            step=step,
            model=self.model,
            attempt=attempt,
            latency_ms=int((time.monotonic() - started) * 1000),
            cost_usd=_cost(input_tokens, output_tokens),
            **fields,
        )
