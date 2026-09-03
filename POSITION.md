# Position

One page on where Indian agentic payments goes, and the specific gap this fills.

---

## The next eighteen months

Three things are already true, and they point the same way.

**Agents are being handed payment credentials right now.** Not in a pilot — in
production, by people wiring an LLM to a card-on-file and a checkout API. The
control surface is a system prompt.

**The rails are moving.** NPCI is piloting UPI Reserve Pay. Razorpay is building
agentic payments with NPCI and OpenAI. Google published AP2 in September 2025
with 60+ organisations behind it. Within eighteen months an Indian consumer will
be able to delegate spending authority to software through a regulated channel.

**Nobody has decided what a merchant does about it.** That is the gap.

## The asymmetry

Almost all the attention is on the **buyer side** — how does a consumer safely
give an agent money. It is the visible problem, the one with a consumer story,
and the one the rails will eventually solve, because that is what rails are for.

The **merchant side** is where the unsolved engineering is:

- An agent presents a mandate. What checks run, in what order, and who wrote them?
- The verifier says no. Is the answer a reason a support agent can read, or a 400?
- A payment times out with unknown outcome. Does the retry double-charge?
- A purchase exceeds the buyer's standing limit. Is there a gate, or a rejection?
- Six months later there is a dispute. Can you prove what was authorised, by whom,
  and that the record has not been edited since?

None of those are protocol questions. AP2 tells you the *format* of a mandate. It
does not tell you how to be a merchant who accepts them safely. Every merchant
integrating agentic payments has to answer all five, and the default answer —
"check the amount, call the API, log a line" — is wrong in ways that surface as
double charges and unwinnable disputes.

**A protocol is not an implementation.** Between "AP2 defines a Payment Mandate"
and "an Indian merchant can accept one without taking on unbounded liability"
there is a gateway, and somebody has to write it.

## What this is

That gateway, for Razorpay, built to the spec:

- **AP2 Merchant + Merchant Payment Processor**, the two roles a merchant needs.
- **Deterministic verification** — 14 checks, no model, measured at p99 0.67 ms.
- **A non-agentic human gate** that returns AP2's own `unresolved_constraint`,
  and where approving authorises one amount at one merchant for one basket for
  ten minutes.
- **Bounded recovery** that survives the case that actually costs money: a
  timeout whose payment succeeded.
- **A tamper-evident audit trail** where the explanation is inside the hash.

And the claim is checkable rather than asserted: 542 tests, 21 red-team attacks
all blocked, 500 benchmarked mandates with zero false accepts, and a third-party
agent that imports none of this code completing a purchase over MCP.

## Why Razorpay specifically

Because the merchant side is where Razorpay already sits. Razorpay is not the
buyer's wallet; it is the merchant's processor. If agentic commerce arrives in
India through UPI, the question every Razorpay merchant asks is *"how do I accept
this safely"* — and the answer has to be infrastructure, not a document.

The three things that would make this production infrastructure rather than a
submission are named in [WHY.md](WHY.md#what-is-the-smallest-real-deployment):
a KMS, Postgres, and the Trusted Surface behind an authenticated session. None of
them touch the verifier.

## The one-sentence version

**The rails will make agentic payments possible; somebody still has to make them
safe for the merchant, and that is a gateway with a deterministic verifier, a
human gate, and an audit trail you can hand to a regulator.**
