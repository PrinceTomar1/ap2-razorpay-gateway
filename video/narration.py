"""The narration, one entry per slide. Written to be spoken, not read."""

SCRIPT = [
    (
        "01_problem",
        """
An A.I. agent holding a payment credential is unbounded liability.
Give a shopping agent a card number, and nothing technical limits what it spends, where,
how many times, or what it does when a payment fails halfway through. Are you sure, is a
control on a human checkout. An agent hits it at machine speed with nobody watching.
Google published the Agent Payments Protocol in September twenty twenty five. There is
no A.P. two implementation for Razorpay. This is that missing piece.
""",
    ),
    (
        "02_mandates",
        """
A.P. two replaces the credential with a mandate. A signed, constrained, verifiable
statement of what the buyer actually authorised.
The buyer signs one open mandate: five thousand rupees a day, fifteen hundred per
purchase, three merchants, twenty four hours, bound to this agent's key.
Every purchase is then a closed mandate signed under it: this much, to them, for this
exact cart, now.
So the agent can shop, and it cannot pay. It holds a keypair and no money.
""",
    ),
    (
        "03_architecture",
        """
Here is the shape. The agent talks to the merchant over M.C.P. The merchant signs the
cart. Then the verifier runs fourteen checks before anything reaches Razorpay.
Signature. Exact V.C.T. claim. Key binding, so a leaked mandate isn't bearer authority.
Payee on the allow list. Amount in range. Running spend within budget. Bound to this
checkout by hash. Nonce not seen before.
Three outcomes. Allow: funds may move, once. Deny: a bound was violated. And
unresolved constraint, which routes to a human.
That third one is the design. An agent that asks gets a human. An agent that forces
gets refused.
""",
    ),
    (
        "04_no_llm",
        """
The load bearing decision is where I did not put a model.
The specification says validation must happen in deterministic code, regardless of
whether the role is agentic. But it is also simply the better engineering.
A verifier is a classifier over a small, fully specified domain. Does this signature
check out. Is this integer below that integer. Code does that perfectly and can explain
exactly what it compared.
And it is not a claim, it is a test. Grep the money path modules for anthropic, or for
the reason writer, and it returns nothing. That grep runs in the suite and fails the
build if it ever stops being empty.
""",
    ),
    (
        "05_demo",
        """
Make demo runs six attempts, offline, with no A.P.I. key and no Razorpay account.
One: the buyer's note named a product that doesn't exist. The agent asks, is told so,
and re-plans. Nothing signed, verifier never ran.
Three: four thousand nine hundred and ninety nine rupees, over the cap. The agent
escalates instead of forcing, and the human declines.
Four: the bank declines U.P.I. Recovery falls back to a payment link and succeeds.
Six: another buyer takes the last item mid flight. Stock is re-read live, clean decline,
and the rail is never contacted.
""",
    ),
    (
        "06_idempotency",
        """
This is the part I would look at if I were judging.
Attempt four created two orders and exactly one capture. Same idempotency root, which
is a S.H.A. two five six of the payment mandate I.D.
And before it creates that second order, it asks the rail whether the first one
captured. Because the genuinely dangerous case in payments is not a decline. It is a
timeout, where you do not know whether the money moved. A naive retry there charges the
buyer twice.
""",
    ),
    (
        "07_audit",
        """
Every money action writes one audit row, with a sentence a person can read.
The rows are hash chained. Each commits to its predecessor, and the explanation is
inside the hash, not beside it. An audit trail where the numbers are tamper evident but
the prose is freely editable is not much of an audit trail.
The tests break that chain six ways, and all six are caught with the damaged row named.
They drop the database triggers first, because a tamper evidence claim you have not
tried to break is a hope, not a property.
""",
    ),
    (
        "08_result",
        """
And the measured result. Six attempts. Four paid. One human denied. One recovered.
Zero rupees unauthorised. Six of six explained.
Every number there is derived, not written. Money is reconciled three ways: the payment
rail, the spend ledger, and the signed receipts. If they disagree, it refuses to print a
report at all.
Five hundred and seventy four tests. Twenty one red team attacks, all blocked. Zero
false accepts in five hundred mandates.
""",
    ),
    (
        "09_limits",
        """
What this is, and what it is not.
Razorpay is implemented against their official S.D.K.: orders, order payments, payment
links, and webhook signature verification. Test mode only.
What is not proved: I never ran it against a real sandbox, because I had no test
credentials. It is correct by review, and not by observation. The README says so.
Sixteen real bugs were found during development, each written up with what it would
have cost. Thank you.
""",
    ),
]
