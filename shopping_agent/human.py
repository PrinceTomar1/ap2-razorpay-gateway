"""The human side of the Trusted Surface, and the seam that keeps it separate.

The agent must be able to *ask* for approval and *read* the answer. It must never
be able to *give* the answer. That boundary is the entire value of the gate, and
in this codebase it is a type boundary rather than a comment:

* The agent holds a :class:`HumanGate` — one method, which returns a status
  dictionary. There is no ``approve()`` on it.
* :class:`SimulatedShopper` holds the :class:`~gateway.trusted_surface.TrustedSurface`
  and therefore the buyer's key. The agent never gets a reference to either.

In a real deployment ``SimulatedShopper`` is a person with a browser and a
passkey. Here it is a callable the demo scripts, so an approval and a denial are
both reproducible. The *decision* is an input to the scenario; everything that
follows from it is computed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from gateway.trusted_surface import HeldRequest, TrustedSurface


class HumanGate(Protocol):
    """What the shopping agent is allowed to know about the approval process."""

    async def await_decision(self, hold_id: str, *, approval_url: str) -> dict[str, Any]:
        """Block until a human answers, then return the surface's public status.

        The returned dictionary contains the mandates a human approval produced,
        if any. It never contains a key, and the agent has no way to produce one.
        """
        ...


#: ``(held request) -> approve?``. In the demo this is a scripted rule; in
#: reality it is somebody reading the page and clicking.
ApprovalPolicy = Callable[[HeldRequest], bool]

#: The bound coroutine a :class:`GateView` wraps.
DecisionFn = Callable[..., Awaitable[dict[str, Any]]]


class GateView:
    """The narrowed handle the agent is actually given.

    :class:`SimulatedShopper` holds the Trusted Surface, and through it the
    buyer's signing key. Handing that object straight to the agent would put an
    ``.surface`` attribute — and therefore ``decide()`` — one dot away from code
    whose whole safety story is that it cannot approve its own payments. A test
    in tests/test_failure_modes.py caught exactly that.

    So the agent gets this instead: one method, one slot, nothing else on it.

    Python cannot make that a hard capability boundary — a determined caller can
    still walk ``__self__`` off a bound method — and pretending otherwise would be
    dishonest. The *hard* boundaries are elsewhere and are tested: the Merchant
    MCP server exposes no tool that approves anything, the agent's keyring
    contains a shopping-agent key and not the buyer's, and the Trusted Surface's
    ``decide()`` is reachable over HTTP only by a form POST from a browser. This
    class is the seam that makes the intent legible in the type signature, so
    that crossing it has to be deliberate rather than accidental.
    """

    __slots__ = ("_decide",)

    def __init__(self, decide: DecisionFn) -> None:
        self._decide = decide

    async def await_decision(self, hold_id: str, *, approval_url: str) -> dict[str, Any]:
        return await self._decide(hold_id, approval_url=approval_url)


def always_approve(_request: HeldRequest) -> bool:
    return True


def always_deny(_request: HeldRequest) -> bool:
    return False


class SimulatedShopper:
    """A person at the Trusted Surface, scripted so a run is reproducible.

    Holds the surface (and so, transitively, the buyer's signing key). The agent
    is handed only the :class:`HumanGate` half of this object.
    """

    def __init__(self, surface: TrustedSurface, policy: ApprovalPolicy = always_deny) -> None:
        self.surface = surface
        self.policy = policy
        #: Every decision made, for the demo's report. ``(hold_id, approved)``.
        self.decisions: list[tuple[str, bool]] = []

    async def await_decision(self, hold_id: str, *, approval_url: str) -> dict[str, Any]:
        """Look at the held request, decide, and return the resulting status.

        The rendered page is generated here even though nothing displays it: it is
        the artefact a real human would read, and generating it proves the amount
        and reason a person would see are the ones the merchant actually signed.
        """
        request = self.surface.get(hold_id)
        _page = self.surface.render(request)  # what the human would be looking at
        approved = self.policy(request)
        self.decisions.append((hold_id, approved))
        decided = self.surface.decide(hold_id, approve=approved)
        return decided.as_dict()

    def gate_view(self) -> GateView:
        """The narrowed handle to give the agent. Never pass ``self``."""
        return GateView(self.await_decision)

    def rendered_page(self, hold_id: str) -> str:
        """The approval page HTML, for the demo to print or a test to assert on."""
        return self.surface.render(self.surface.get(hold_id))
