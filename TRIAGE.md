# Triage

Every command in the triage list, run before anything was changed.

**Finding: nothing is broken.** All seven commands succeed. The one real defect is
a single-character mismatch in the report line, fixed immediately below.

| # | Command | Result |
|---|---|---|
| 1 | `make setup` | ✅ `setup complete — Python 3.13.5` |
| 2 | `make test` | ✅ `570 passed in 4.52s` · 0 skipped · 0 xfail |
| 3 | `ruff check .` | ✅ `All checks passed!` |
| 4 | `mypy gateway merchant shopping_agent` | ✅ `Success: no issues found in 24 source files` |
| 5 | `make demo` | ⚠️ runs, but prints `Rs 0` where the spec says `₹0` |
| 6 | `make mcp` | ✅ FastMCP server starts on stdio |
| 7 | `make serve` | ✅ boots, `/health` → `audit_chain_intact: true` |

```
$ make test
570 passed in 4.52s

$ ruff check .
All checks passed!

$ mypy gateway merchant shopping_agent
Success: no issues found in 24 source files

$ make serve
{"status":"ok","rail":"fake","narration":"fake","catalogue_skus":60,
 "audit_rows":0,"audit_chain_intact":true}
```

## The one defect

| | |
|---|---|
| **Spec** | `6 attempts · 4 paid · 1 human-denied · 1 recovered · ₹0 unauthorised · 6/6 explained` |
| **Printed** | `6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained` |

`Rs 0` where the specification says `₹0`. It matters because this line is read
aloud off the screen in the video, and because `tests/test_docs.py` pins it
byte-for-byte across four documents — so a mismatch here is drift by definition.

Fixed by changing the single format string in `demo/batch.py::Report.line()` and
propagating to every document and test that quotes it.

## Nothing else was changed

The dependency-order repair list (mandates → verify → audit → payments → recovery
→ merchant → trusted surface → agent → demo) applies to a broken build. This one
is not broken, so it collapses to: fix the report line, re-prove the gate, verify
the documents.
