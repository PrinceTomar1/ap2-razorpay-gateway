"""The only place a language model runs.

Two jobs, both off the money path:

* **Narration** (:mod:`llm.reason`) — the plain-English "why" beside each audit
  row. If the model is unreachable, a deterministic template is written instead
  and the transaction proceeds unchanged.
* **Product selection** (:mod:`shopping_agent.agent`, ``--llm`` mode) — which SKU
  to look at, out of search results the merchant returned. The model can pick a
  bad shoe. It cannot pick a bad *amount*, a bad payee, or a second charge,
  because every one of those is decided in gateway/verify.py from signed data.

Nothing in ``gateway/verify.py``, ``gateway/payments.py`` or
``gateway/recovery.py`` imports this package. That is asserted by a test, not
merely intended.
"""
