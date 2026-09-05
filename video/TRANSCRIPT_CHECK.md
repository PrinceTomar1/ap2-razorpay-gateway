# Stage 1 — Corrected transcript

Transcribed with whisper.cpp `small.en` (beam 8), then checked line by line
against `narration.py` and the slide each clip plays over.

**Two passes agreed on every ambiguous phrase**, so the readings below are what
is actually on the tape, not a single model's guess.

---

## `00` — intro  ·  0:00 – 00:15

> Hi, I I'm Prince Thomer, Authority of Beta Computer Science Student at Bennett
> University Greater Noida. This is my submission for track 1 of the Razorpay AI
> buildathon and implementation of Google's Agent Payments Protocol for Razorpay.

**Transcription fixed** (speaker was correct, Whisper was not): `am` → `I'm`, `Tomer` → `Tomar`, `beta` → `B.Tech`, `Benet` → `Bennett`, `Craternoida` → `Greater Noida`, `AI build thon` → `AI Buildathon`

---

## `01` — problem  ·  0:00 – 00:32

> An AI agent holding a payment credential is unbounded liability. Give a shopping
> agent a card number and nothing technical limits what it spends, where, how many
> times or what it does when a payment fails halfway through. Are you sure? Is a
> control on a human checkout? An agent hits it at machine speed with nobody watching.
> Google published the Agent Payments Protocol in September 2025. There is no AP2
> implementation for user pay. This is that missing part.

**Transcription fixed** (speaker was correct, Whisper was not): `car number` → `card number`, `AP2` → `AP2`

**Departed from script:**

- script: *this is that missing piece*
  spoken: **this is that missing part**
  → harmless synonym

---

## `02` — mandates  ·  0:00 – 00:31

> AP2 replaces the credential with a mandate, a signed constrained verifiable its
> statement of what the buyer actually authorized. The buyer signs one open mandate,
> 5000 rupees a day, 1500 per purchase, 3 merchants, 24 hours bound to that agent's
> key, every purchase is then a closed mandate signed under it. This much to them for
> this exact cart now, so the agent can shop and it cannot pay, it It holds a keypair
> and no money.

**Transcription fixed** (speaker was correct, Whisper was not): `constraint` → `constrained`, `key pair` → `keypair`

---

## `03` — architecture  ·  0:00 – 00:43

> Here is the shape. The agent talks to the merchant over MCP. The merchant signs the
> cart. Then the verifier runs 14 checks before anything reaches Razorpay. Signature
> exact VCT claim key binding so a leaked mandate is not bearer authority. Pay on the
> allow list amount in range. Spend within range. Bound to this checkout by hash. Nons
> not seen before. Three outcomes. Allow. Once may move. Once deny. A bound was
> violated and unresolved. Constraint. Which routes to a human. That third one is the
> design. An agent that asks gets a human. An agent that forces gets refused.

**Transcription fixed** (speaker was correct, Whisper was not): `card` → `cart`, `cheques` → `checks`, `reserve pay` → `Razorpay`, `execs VCT` → `exact VCT`, `nons` → `nonce`, `the checklist` → `this checkout`, `once may move` → `funds may move`, `notes` → `routes`

**Departed from script:**

- script: *Amount in range. Spend within budget.*
  spoken: **Amount in range. Spend within range.**
  → 'range' said twice — repetitive, and 'budget' is the more precise word

---

## `04` — no llm  ·  0:00 – 00:44

> The load-bearing decision is where I did not put a model. The specification says
> validation must happen in deterministic code. But it is also simply the better
> engineering. A verifier is a classifier over a small, fully specified domain. Does
> this signature check out? Is this integer below that integer? Code does that
> perfectly and can explain exactly what it compared. And it is not a claim, it is a
> test. Grab the money path modules for Antropic or for the Reason writer. And it
> returns nothing that grab runs in the suite and fails the build if it ever stops
> being empty.

**Transcription fixed** (speaker was correct, Whisper was not): `Get the money path` → `Grep the money path`, `entropic` → `Anthropic`, `that crap runs` → `that grep runs`, `suit` → `suite`

**Departed from script:**

- script: *must happen in deterministic code, regardless of whether the role is agentic*
  spoken: **must happen in deterministic code**
  → clause dropped — tighter, no meaning lost

---

## `05` — demo  ·  0:00 – 00:42

> MakeDemo runs six attempts, offline, with no API key and no Razorpay account. 1. The
> buyer's note noted a product that does not exist. The agent asks, "Is it?" told so,
> and re-plans, "Nothing signed, Verifier never ran." 3. 4,999 rupees over the cap.
> The agent escalates instead of forcing and the human declines. 4. The bank declines
> UPI recovery falls, back to a payment link and succeeds. 6. Nothing buyer takes the
> last item, mid-flight, stock is reread, live, clean decline and the rail is never
> contacted.

**Transcription fixed** (speaker was correct, Whisper was not): `makedemo` → `make demo`, `recipe` → `Razorpay`, `replains` → `re-plans`, `verify` → `verifier`, `folds` → `falls`, `nothing buyer` → `another buyer`, `claim decline` → `clean decline`, `compacted` → `contacted`

---

## `06` — idempotency  ·  0:00 – 00:34

> This is the part I would look if I were judging attempt four created two orders and
> exactly one capture. Same idempotency root which is a SHA-256 of the payment mandate
> ID and before it creates that second order, it asks the rail whether the first one
> captured because the genuinely dangerous case in payments is not a decline. It is a
> timeout where you do not know whether the money moved a naive retry there charges
> the buyer twice.

**Transcription fixed** (speaker was correct, Whisper was not): `route` → `root`, `SHA 256` → `SHA-256`, `real` → `rail`, `and if retried` → `a naive retry`, `mode` → `moved`

---

## `07` — audit  ·  0:00 – 00:36

> Every money action writes one audit row. With a sentence a person can read, "The
> rows are hash-chained and the explanation is inside the hash." Not beside it. An
> audit trail where the numbers are tamper-evident but the prose is freely editable is
> not much of an audit trail. The test break that chain six ways and all six are
> caught with a damaged row named. They drop the database triggers first because a
> tamper evidence claim you have no tried to break is a hope not a property.

**Transcription fixed** (speaker was correct, Whisper was not): `audit rail` → `audit trail`, `group the database` → `drop the database`

---

## `08` — result  ·  0:00 – 00:32

> And the measured result, 6 attempts, 4 paid, 1 human denied, 1 recovered, 0 rupees
> unauthorised, 6 of 6 explained every number there's derived, not written, money as
> reconciled, 3 ways the payment rail, the spend ledger and the signed receipts. If
> they disagree, it refuses to print a report at all, 574 test, 21 red team attacks,
> all block, zero false accepts in 500 mandates.

**Transcription fixed** (speaker was correct, Whisper was not): `reel` → `rail`, `results` → `zero false accepts`

---

## `09` — limits  ·  0:00 – 00:33

> What this is and what it is not Resurby is implemented against their official SDK
> orders, order payments, payment links and webhook, signature verification, test mode
> only. What is not proved? I never ran it against a real sandbox because I had no
> test credentials. It is correct by review and not by observation. The readme says
> so. 16 real bugs were found during deployment, each written up with what it would
> have caused. Thank you.

**Transcription fixed** (speaker was correct, Whisper was not): `Reservice` → `Razorpay is`, `HDK` → `SDK`

**Departed from script:**

- script: *found during development*
  spoken: **found during deployment**
  → wrong word: the bugs were found while building, not while deploying

- script: *what it would have cost*
  spoken: **what it would have caused**
  → 'cost' is the intended sense — what the bug would have cost

---

---

## What was done to the audio

Measured, not asserted:

| | |
|---|---|
| room tone in the pauses | **−52.3 → −56.8 dB** (clips 01/03/09; 4–5 dB of noise removed) |
| finished file, integrated | **−14.2 LUFS** |
| finished file, true peak | **−1.3 dBTP** |
| A/V drift | **25 ms over 297 s** — under one frame at 30 fps |
| slide fidelity | SSIM ≥ 0.9997 against the source PNGs, all ten |

### The claim that did *not* hold up

The chain was expected to make words measurably easier to recognise. It did not.
Word error rate against `narration.py`, whisper.cpp `small.en`, all 847 words:

| chain | WER |
|---|---|
| high-pass + denoise + de-ess + compress + presence (shipped) | 19.72% |
| high-pass + denoise only | 19.72% |
| high-pass + denoise + compress + presence | 19.83% |
| **raw, untouched** | **19.95%** |
| gentler settings throughout | 20.31% |

A 0.6-point spread across five chains including the raw file is noise, not signal.
The errors are dominated by vocabulary the model does not have — *Razorpay*, *AP2*,
*JWS*, *idempotency*, *Bennett*, *Tomar* — and no amount of EQ teaches it those.

The chain is kept because the two things it *does* do are real and measured: the
noise floor drops 4–5 dB, and the file sits at the level YouTube targets so
playback normalisation leaves it alone. What is not claimed is that it made the
speech more intelligible, because the one instrument available says it did not.

The recordings were already clean — a −52 dB noise floor going in. There was very
little room to improve, which is the honest reason the numbers barely move.
