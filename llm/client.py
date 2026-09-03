"""A one-method LLM interface, with a real client and a fake one.

The interface is deliberately tiny. Everything this project asks a model to do
fits in "here is some context, give me one sentence" — and an interface that
small is one you can swap, stub, or delete without touching anything else. That
is the property that makes the "no LLM on the money path" claim checkable rather
than aspirational: there is exactly one door, and the money path does not have a
key to it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

#: A small, fast model. Narration is a one-sentence rewrite on a background path;
#: paying for a frontier model to do it would be a waste, and a slow one would
#: make the audit write wait on a network call.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

#: Hard cap. Narration that runs long is narration nobody reads.
DEFAULT_MAX_TOKENS = 150

#: Short by design. Anything on this path that blocks for longer than this should
#: fall back to the template instead — an audit row must never wait on a model.
DEFAULT_TIMEOUT_SECONDS = 8.0

#: The label :class:`llm.reason.ReasonWriter` puts on the deterministic draft it
#: hands the model. FakeLLM finds it and returns the draft unchanged.
DRAFT_PREFIX = "Draft: "


class LLMUnavailable(RuntimeError):
    """The model could not be reached, or refused. Always recoverable."""


@runtime_checkable
class LLMClient(Protocol):
    name: str

    def complete(self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        """Return one short completion. Raise :class:`LLMUnavailable` on failure."""
        ...


class FakeLLM:
    """A deterministic stand-in. No network, no key, no variance.

    Used by every test and by ``make demo``. It can be scripted with fixed
    responses, or told to fail — which is how the fallback path in
    :mod:`llm.reason` gets exercised rather than assumed.
    """

    name = "fake"

    def __init__(
        self,
        responses: Sequence[str] | None = None,
        *,
        fail: bool = False,
        echo_draft: bool = True,
    ) -> None:
        self._responses = list(responses or [])
        self._index = 0
        self.fail = fail
        self.echo_draft = echo_draft
        #: Every prompt it was given. Tests assert on this to prove the model was
        #: consulted for narration and *not* consulted for anything else.
        self.calls: list[tuple[str, str]] = []

    def complete(self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        self.calls.append((system, prompt))
        if self.fail:
            raise LLMUnavailable("FakeLLM was configured to fail")
        if self._responses:
            response = self._responses[self._index % len(self._responses)]
            self._index += 1
            return response
        if self.echo_draft:
            # Return the deterministic draft it was handed, unchanged. In offline
            # mode the narration you read is therefore exactly the template —
            # honest about what produced it, rather than inventing prose that no
            # model actually wrote.
            for line in reversed(prompt.strip().splitlines()):
                if line.startswith(DRAFT_PREFIX):
                    return line[len(DRAFT_PREFIX) :].strip()
        return ""


class AnthropicClient:
    """The real client. Constructed only when ``ANTHROPIC_API_KEY`` is present."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise LLMUnavailable(
                "ANTHROPIC_API_KEY is not set. Narration will fall back to templates; "
                "nothing else changes."
            )
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.Anthropic(api_key=key, timeout=timeout_seconds)
        self.model = model

    def complete(self, *, system: str, prompt: str, max_tokens: int = DEFAULT_MAX_TOKENS) -> str:
        try:
            message = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:
            # Deliberately broad. Whatever went wrong — network, rate limit, auth,
            # a new SDK exception type — the answer is the same: fall back to the
            # template. A narration failure must never become a payment failure.
            raise LLMUnavailable(f"Anthropic call failed: {exc}") from exc

        parts = [
            block.text
            for block in message.content
            if getattr(block, "type", None) == "text" and hasattr(block, "text")
        ]
        text = " ".join(parts).strip()
        if not text:
            raise LLMUnavailable("Anthropic returned no text")
        return text


def build_llm(provider: str | None = None) -> LLMClient:
    """Build the client named by ``$LLM_PROVIDER``. Defaults to ``fake``.

    Defaulting to the fake is the same reasoning as defaulting the payment rail
    to the simulator: the dangerous default is the one that reaches for a
    credential and a network when nobody asked it to.
    """
    selected = (provider or os.environ.get("LLM_PROVIDER") or "fake").strip().lower()
    if selected == "fake":
        return FakeLLM()
    if selected == "anthropic":
        return AnthropicClient(model=os.environ.get("LLM_MODEL") or DEFAULT_MODEL)
    raise ValueError(f"unknown LLM_PROVIDER {selected!r}; expected 'fake' or 'anthropic'")
