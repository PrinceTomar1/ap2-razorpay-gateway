# The live Razorpay check

Everything in this project runs offline by default. This document covers the one
optional step: pointing attempts 1 and 4 at a real Razorpay **test-mode** sandbox.

You do not need this to evaluate the project. `make demo` exercises the same
verifier, the same idempotency, the same recovery playbook and the same audit chain
against `FakeRail`. What the live check adds is proof that `RazorpayRail` really
speaks to Razorpay.

> **Test mode only.** `RazorpayRail.__init__` raises if `RAZORPAY_KEY_ID` does not
> start with `rzp_test_`. There is no live path in this repository, deliberately.

---

## 1. Get test keys

1. Sign in at <https://dashboard.razorpay.com>.
2. Switch the toggle at the top to **Test Mode**. Confirm it says Test Mode before
   you continue.
3. **Settings → API Keys → Generate Test Key**.
4. Copy the Key Id (`rzp_test_…`) and the Key Secret. The secret is shown once.

Put them in `.env`:

```bash
cp .env.example .env
```

```dotenv
PAYMENT_RAIL=razorpay
RAZORPAY_KEY_ID=rzp_test_XXXXXXXXXXXXXX
RAZORPAY_KEY_SECRET=your_test_secret
```

`.env` is gitignored. Nothing in this repository ever writes a key to disk or logs
one.

## 2. Run the live check

```bash
make demo LIVE=1
```

This runs **attempts 1 and 4 only** — one clean purchase and one recovery. The rest
of the batch is about behaviour a real sandbox cannot be made to produce on demand
(a timeout at a chosen moment, a shelf emptying mid-flight).

For each payment the gateway will:

1. `POST /v1/orders` — create an order for the exact amount in the signed cart.
2. `POST /v1/payment_links` — create a hosted payment link for that order.
3. Print the link and **poll** `GET /v1/orders/{id}/payments` until a terminal
   payment appears, or `RAZORPAY_POLL_TIMEOUT_SECONDS` (default 180) elapses.

You will see something like:

```
  Razorpay test mode — pay this link to continue:
    https://rzp.io/i/AbCdEf123
    UPI id success@razorpay to succeed, failure@razorpay to decline.
    Waiting up to 180s...
```

Open the link, choose **UPI**, and enter:

| VPA | Outcome |
|---|---|
| `success@razorpay` | payment captured |
| `failure@razorpay` | payment failed |

**Attempt 1:** pay with `success@razorpay`. Expect a captured payment and a signed
receipt.

**Attempt 4:** pay with **`failure@razorpay`** on the first link. The recovery
playbook will log the decline, fall back to the next instrument and give you a second
link — pay that one with `success@razorpay`. Expect exactly one capture, under the
same idempotency root, and a `recovery.succeeded` audit row.

Test cards work too, if you would rather not use UPI:

| Card | Outcome |
|---|---|
| `4111 1111 1111 1111` | success |
| `5104 0600 0000 0008` | success (Mastercard) |
| Any future expiry, any CVV, OTP **`754081`** | |

Razorpay's full list: <https://razorpay.com/docs/payments/payments/test-card-details/>

### Why there is no "just pay it from the API" flag

**There is no Razorpay API that completes a payment on a customer's behalf.** A
payment is made by a human on Razorpay's hosted page. We could have written something
that looked like one; it would have been an invented API. So the live path does the
honest thing — create a link, print it, poll. See
[LIMITATIONS.md](../LIMITATIONS.md#complete_test_payment-on-the-real-rail-polls-it-does-not-pay).

---

## 3. Webhooks (optional)

**You do not need webhooks.** The payment path already polls `order.payments`, which
is the same source of truth Razorpay uses to *build* the webhook. Webhooks are
faster; polling is sufficient, and it is what `LIVE=1` uses.

If you want them anyway — to see `mpp.webhook_received` rows in the audit trail —
you need a public URL.

### 3a. Tunnel

```bash
make serve                      # terminal 1 — gateway on :8000
ngrok http 8000                 # terminal 2
```

Copy the `https://….ngrok-free.app` URL ngrok prints.

### 3b. Register the webhook

1. Dashboard (still in **Test Mode**) → **Settings → Webhooks → Add New Webhook**.
2. **Webhook URL:** `https://<your-ngrok-host>/webhooks/razorpay`
3. **Secret:** choose one. Put the same string in `.env` as
   `RAZORPAY_WEBHOOK_SECRET`.
4. **Active Events:** tick `payment.captured`, `payment.failed`, `order.paid`,
   `payment_link.paid`.
5. Save. Razorpay sends an OTP to the account owner to confirm the change — in test
   mode this is **`754081`**.

Restart `make serve` so it picks up the secret. Check it:

```bash
curl -s http://127.0.0.1:8000/webhooks/razorpay/health
# {"configured": true, ...}
```

### 3c. What the gateway does with one

1. **Verifies the signature first**, over the *raw request body*, HMAC-SHA256 with
   your secret, compared with `hmac.compare_digest`. Nothing is parsed before this.
2. A bad or missing signature returns **400** — so Razorpay retries — and writes an
   `mpp.webhook_signature_rejected` row to the audit chain. A stream of those is
   something an operator wants to see.
3. A verified webhook is recorded as **information**. It resolves a pending payment;
   it can never authorise one. Authorisation happened in `gateway/verify.py`, before
   the order existed.

You can test the rejection path without Razorpay:

```bash
curl -i -X POST http://127.0.0.1:8000/webhooks/razorpay \
     -H 'X-Razorpay-Signature: definitely-not-valid' \
     -d '{"event":"payment.captured"}'
# HTTP/1.1 400 Bad Request
```

---

## 4. Verifying what happened

**In the Razorpay dashboard** (Transactions → Orders): every order's *receipt* field
is the idempotency key for the mandate that authorised it, and its *notes* carry the
payment mandate id, the checkout hash and `protocol: ap2-v0.2`. So any order can be
traced back to the signed mandate that produced it without touching our database.

**In the gateway:**

```bash
curl -s http://127.0.0.1:8000/audit | python3 -m json.tool | head -40
```

`verified: true` means the hash chain is intact. `tip_hash` is the value to record if
you want to detect later truncation.

**In `demo/report.json`:** `rail` will read `razorpay` and `live` will be `true`.

---

## 5. Troubleshooting

| Symptom | Cause |
|---|---|
| `refusing to start: RAZORPAY_KEY_ID is 'rzp_live_…'` | A live key. This project will not accept one. Regenerate in Test Mode. |
| `PAYMENT_RAIL=razorpay needs RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET` | `.env` missing or not loaded. `cp .env.example .env` and fill it in. |
| `BAD_REQUEST_ERROR: Authentication failed` | Key id and secret from different keypairs, or a live key with a test secret. |
| Poller times out | Nobody paid the link. Raise `RAZORPAY_POLL_TIMEOUT_SECONDS`, or pay faster. A timeout is treated as *outcome unknown*, not a decline — the next attempt will probe the order before creating another. |
| Webhook never arrives | ngrok URL changed (free tier rotates on restart). Update it in the dashboard. |
| `invalid webhook signature` | `RAZORPAY_WEBHOOK_SECRET` does not match the dashboard, or a proxy rewrote the body. The signature is over the exact bytes. |
| Dashboard asks for an OTP | Test-mode OTP is **`754081`**. |

---

## 6. Environment reference

| Variable | Default | Used when |
|---|---|---|
| `PAYMENT_RAIL` | `fake` | always |
| `RAZORPAY_KEY_ID` | — | `PAYMENT_RAIL=razorpay` |
| `RAZORPAY_KEY_SECRET` | — | `PAYMENT_RAIL=razorpay` |
| `RAZORPAY_WEBHOOK_SECRET` | *(empty)* | webhooks only; empty disables them |
| `RAZORPAY_POLL_TIMEOUT_SECONDS` | `180` | live rail |
| `RAZORPAY_POLL_INTERVAL_SECONDS` | `3` | live rail |
| `GATEWAY_PUBLIC_URL` | `http://127.0.0.1:8000` | Trusted Surface approval links |

Full list with comments: [`.env.example`](../.env.example).
