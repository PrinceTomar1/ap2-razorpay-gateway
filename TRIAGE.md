# Triage

Every command in the brief's triage list, run before anything was changed, with
its real output. Written first so that what follows is repair rather than
guesswork.

**Finding: the existing build has no defects.** Every gate passes, and a fresh
clone runs the demo offline. I am not going to invent bugs to look diligent.

What is genuinely absent is the **newly requested scope** — interop, red-team,
benchmark, the HTML audit timeline, and eight documents that did not exist
before. That is the real work, and §3 lists it.

---

## 1. Commands run, with output

| # | Command | Result |
|---|---|---|
| T1 | `make setup` | ✅ `setup complete — Python 3.13.5` |
| T2 | `make test` | ✅ `519 passed in 5.67s` · 0 skipped · 0 xfail |
| T3 | `ruff check .` | ✅ `All checks passed!` |
| T4 | `mypy --strict gateway merchant shopping_agent` | ✅ `Success: no issues found in 23 source files` |
| T5 | `make demo` ×2 | ✅ identical exact report line both runs |
| T6 | `make mcp` | ✅ FastMCP 4.0.1 starts on stdio, 60 SKUs, 3 merchants |
| T7 | `make serve` | ✅ boots, `/health` → `{"status":"ok",…,"audit_chain_intact":true}` |
| T8 | fresh clone in `/tmp/acg` | ✅ `make setup && make demo` → exact report line |

```
$ make test
519 passed in 5.67s

$ ruff check .
All checks passed!

$ mypy --strict gateway merchant shopping_agent
Success: no issues found in 23 source files

$ make demo   # run 1
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
$ make demo   # run 2
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained

$ make serve
{"status":"ok","rail":"fake","narration":"fake","catalogue_skus":60,
 "audit_rows":17,"audit_chain_intact":true}

$ cp -r . /tmp/acg && cd /tmp/acg && rm -rf .venv *.db __pycache__ \
    && cp .env.example .env && make setup && make demo
setup complete — Python 3.13.5
6 attempts · 4 paid · 1 human-denied · 1 recovered · Rs 0 unauthorised · 6/6 explained
```

## 2. The one real error found

```
$ mypy --strict gateway merchant shopping_agent verify demo
mypy: error: Cannot read file 'verify': No such file or directory
```

`verify` is not a top-level package — the verifier is `gateway/verify.py`, and it
is already covered by the `gateway` argument. The command in the brief names a
path that has never existed. The equivalent correct command passes:

```
$ mypy --strict gateway merchant shopping_agent demo
Success: no issues found in 25 source files
```

Nothing was changed for this. Renaming a module to match a typo in an
instruction would be the wrong repair.

## 3. What is actually missing — the new scope

Absent before this pass, because it was not previously asked for:

| Missing | Kind |
|---|---|
| `make interop` · `scenarios/ap2_reference/` | Standout layer |
| `make redteam` · `redteam/` · `REDTEAM.md` | Standout layer |
| `make bench` · `bench/run.py` · `BENCHMARK.md` | Standout layer |
| `demo/audit_chain.html` | Demo artefact |
| `CONFORMANCE.md` | AP2 evidence |
| `WHY.md` · `POSITION.md` · `STANDOUT.md` | Narrative |
| `WORKING_PROOF.md` · `TRIAGE.md` | Proof |
| README "For reviewers — 90 seconds" block | Zero friction |

## 4. Order of work

The brief's dependency order (mandates → verify → audit → payments → recovery →
merchant → trusted surface → agent → demo → docs → standout) applies to a broken
build. This one is not broken, so the order collapses to: **build the missing
layers, then re-prove the whole gate.**

Each layer is additive and gated on the core staying green — `make test`, `ruff`,
`mypy --strict` and `make demo` are re-run after every one. Anything that cannot
be made to work reliably gets cut and recorded in LIMITATIONS.md, per the brief's
own rule that a smaller thing which fully works beats a bigger thing that
half-works.
