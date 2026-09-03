"""One plain-English sentence explaining why something happened.

Track 01 asks for every money action to be *explainable*. That has two halves,
and they are not the same thing:

* The **machine** explanation — which checks ran, on what numbers, in what order.
  That is :class:`gateway.verify.Decision`, it is deterministic, and it is the
  authoritative one.
* The **human** explanation — one sentence a person can read in a dispute six
  months later without knowing what a ``vct`` claim is. That is this module.

The important design property is the direction of dependency. The template is
computed first, from the facts, deterministically. The model is then *optionally*
asked to phrase it better. If there is no key, no network, no quota, or the SDK
raises something we have never seen, the template is what gets written and the
transaction is completely unaffected.

    reason() never raises. Not "rarely raises" — never.

That is enforced by a bare ``except Exception`` around the model call and by a
test that hands it a client which throws. An audit row that fails to write
because a language model was down would be a worse outcome than no narration at
all, and it is a genuinely easy mistake to make.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from ap2_min.models import paise_to_inr_str
from gateway.audit import Event
from gateway.mandates import canonical_json
from llm.client import DEFAULT_MAX_TOKENS, DRAFT_PREFIX, LLMClient

SYSTEM_PROMPT = (
    "You explain payment-system events to a non-technical person in ONE short "
    "sentence, under 25 words. You are given the facts and a draft sentence. "
    "Rewrite the draft to be clearer and more natural. Never invent a fact that "
    "is not in the input. Never add advice, apologies, or a second sentence. "
    "Reply with the sentence only."
)


def _money(payload: Mapping[str, Any], key: str, default: str = "the amount") -> str:
    value = payload.get(key)
    return f"₹{paise_to_inr_str(int(value))}" if isinstance(value, int) else default


#: Deterministic fallbacks. Every one of these is what actually gets written when
#: no model is configured, which is the default — so they are written to be read,
#: not to be placeholders.
TEMPLATES: dict[str, str] = {
    Event.CART_ASSEMBLED: "Assembled a cart of {item_count} item(s) from {merchant} for {total}.",
    Event.CHECKOUT_CREATED: (
        "Signed a checkout for {total} at {merchant}, guaranteed at that price for {ttl_minutes} minutes."
    ),
    Event.CHECKOUT_MANDATE_RECEIVED: (
        "The agent presented the buyer's standing checkout authorisation for a {total} cart."
    ),
    Event.CHECKOUT_RECEIPT_ISSUED: "Checkout confirmed for {total} at {merchant}.",
    Event.CHECKOUT_UNRESOLVED: "This checkout needs the buyer's approval: {reason}",
    Event.PRODUCT_NOT_FOUND: "The agent asked for {sku}, which does not exist in this catalogue.",
    Event.STOCK_RECHECK_FAILED: "Stopped before charging: {reason}",
    Event.STOCK_DECREMENTED: "Reduced stock after payment cleared: {detail}",
    Event.PAYMENT_MANDATE_RECEIVED: (
        "Received a payment mandate for {amount} to {payee}, signed by the buyer's agent."
    ),
    Event.DECISION: "{outcome}: {reason}",
    Event.MANDATE_REJECTED: "Rejected at the boundary: {reason}",
    Event.GATE_REQUESTED: "Asked the buyer to approve {amount} at {merchant}, because {reason}",
    Event.GATE_APPROVED: "The buyer approved {amount} at {merchant}.",
    Event.GATE_DENIED: "The buyer declined {amount} at {merchant}. Nothing was charged.",
    Event.GATE_EXPIRED: "The approval request for {amount} expired before the buyer answered.",
    Event.AGENT_PLAN: "The agent is looking for: {goal}",
    Event.AGENT_REPLANNED: "The agent changed plan: {reason}",
    Event.AGENT_ESCALATED: "The agent stopped and asked for approval: {reason}",
    Event.AGENT_GAVE_UP: "The agent abandoned this purchase: {reason}",
}

GENERIC_TEMPLATE = "{event} recorded."


def render_template(event: str, payload: Mapping[str, Any]) -> str:
    """The deterministic sentence for an event. Never raises, never blocks.

    A missing key falls back to the generic form rather than throwing: narration
    is best-effort by construction, and a ``KeyError`` here would take down the
    audit write it exists to decorate.
    """
    template = TEMPLATES.get(event, GENERIC_TEMPLATE)
    fields = dict(payload)
    fields.setdefault("event", event.replace(".", " ").replace("_", " "))
    for key in ("amount", "total", "budget", "limit"):
        if isinstance(fields.get(key), int):
            fields[key] = _money(payload, key)
    try:
        return template.format(**fields)
    except (KeyError, IndexError, ValueError):
        return GENERIC_TEMPLATE.format(event=fields["event"])


class ReasonWriter:
    """Turns an event into one sentence, with a model if there is one.

    Caching is by ``sha256(event + canonical(payload))``. The same event with the
    same facts is narrated once, which matters because the demo replays similar
    events six times and paying for six identical completions would be silly.
    """

    def __init__(
        self,
        client: LLMClient | None = None,
        *,
        enabled: bool = True,
        cache: dict[str, str] | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.client = client
        self.enabled = enabled and client is not None
        self._cache: dict[str, str] = cache if cache is not None else {}
        self.max_tokens = max_tokens
        #: Counters a run can report honestly: how often the model was actually
        #: used, and how often we fell back.
        self.stats = {"template": 0, "model": 0, "cache": 0, "fallback": 0}

    @staticmethod
    def cache_key(event: str, payload: Mapping[str, Any]) -> str:
        return hashlib.sha256((event + canonical_json(dict(payload))).encode("utf-8")).hexdigest()

    def reason(self, event: str, payload: Mapping[str, Any] | None = None) -> str:
        """One sentence. Guaranteed to return, guaranteed not to raise."""
        facts = dict(payload or {})
        templated = render_template(event, facts)

        if not self.enabled or self.client is None:
            self.stats["template"] += 1
            return templated

        key = self.cache_key(event, facts)
        cached = self._cache.get(key)
        if cached is not None:
            self.stats["cache"] += 1
            return cached

        prompt = f"Event: {event}\nFacts: {canonical_json(facts)}\n{DRAFT_PREFIX}{templated}"
        try:
            text = self.client.complete(
                system=SYSTEM_PROMPT, prompt=prompt, max_tokens=self.max_tokens
            ).strip()
        except Exception:  # noqa: BLE001 — see the module docstring; this must never propagate
            self.stats["fallback"] += 1
            return templated

        if not text:
            self.stats["fallback"] += 1
            return templated

        # A model that ignored the length instruction gets truncated rather than
        # trusted. Narration is a fixed-width column, not free text.
        sentence = text.splitlines()[0].strip()
        if len(sentence) > 240:
            sentence = sentence[:237].rstrip() + "…"
        self._cache[key] = sentence
        self.stats["model"] += 1
        return sentence


def template_only() -> ReasonWriter:
    """A writer that never calls a model. The default everywhere on the money path."""
    return ReasonWriter(client=None, enabled=False)
