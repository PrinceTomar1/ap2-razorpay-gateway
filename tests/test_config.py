"""Loading `.env`, and the configuration errors a human is meant to fix.

This module exists because of a bug found during adversarial review: every
document told the reader to put their Razorpay keys in `.env`, `make setup`
created one, and nothing read it. The live check would have failed with a
traceback instructing the reader to do the thing they had already done.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from gateway.config import ConfigurationError, load_dotenv, parse_env
from gateway.razorpay_client import FakeRail, RazorpayRail, build_rail

# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parses_plain_assignments() -> None:
    assert parse_env("A=1\nB=two\n") == {"A": "1", "B": "two"}


def test_ignores_comments_and_blank_lines() -> None:
    text = "\n# a comment\n\nA=1\n   # indented comment\nB=2\n"
    assert parse_env(text) == {"A": "1", "B": "2"}


def test_strips_surrounding_whitespace() -> None:
    assert parse_env("  A  =  spaced  \n") == {"A": "spaced"}


def test_strips_exactly_one_layer_of_matching_quotes() -> None:
    parsed = parse_env("""A="dq"\nB='sq'\nC="mixed'\nD=""\nE='''\n""")
    assert parsed["A"] == "dq"
    assert parsed["B"] == "sq"
    assert parsed["C"] == "\"mixed'", "mismatched quotes are literal"
    assert parsed["D"] == ""
    assert parsed["E"] == "'"


def test_accepts_the_export_prefix() -> None:
    """Hand-written .env files often carry `export`. Silently creating a variable
    named "export FOO" would be worse than accepting it."""
    assert parse_env("export RAZORPAY_KEY_ID=rzp_test_abc\n") == {"RAZORPAY_KEY_ID": "rzp_test_abc"}


def test_a_value_containing_equals_is_kept_whole() -> None:
    """Base64 secrets end in `=`. Splitting on the last `=` would corrupt them."""
    assert parse_env("SECRET=abc==\n") == {"SECRET": "abc=="}
    assert parse_env("URL=http://x/?a=1&b=2\n") == {"URL": "http://x/?a=1&b=2"}


def test_lines_without_an_equals_are_skipped() -> None:
    assert parse_env("just some text\nA=1\n") == {"A": "1"}


def test_an_empty_key_is_skipped() -> None:
    assert parse_env("=orphan\nA=1\n") == {"A": "1"}


def test_nothing_is_evaluated() -> None:
    """A .env is data. It must never be able to execute or interpolate anything.

    Command substitution, variable interpolation and backticks all stay literal
    text, so a `.env` written by someone else cannot run code or leak an existing
    environment variable into a value.
    """
    parsed = parse_env(
        "A=$(rm -rf /)\nB=`whoami`\nC=${HOME}\nD=$PATH\n",
    )
    assert parsed["A"] == "$(rm -rf /)"
    assert parsed["B"] == "`whoami`"
    assert parsed["C"] == "${HOME}"
    assert parsed["D"] == "$PATH"


def test_multiline_values_are_not_supported_and_do_not_bleed() -> None:
    """A quoted value spanning lines is not joined; the continuation is ignored."""
    parsed = parse_env('A="first\nsecond"\nB=2\n')
    assert parsed["B"] == "2"
    assert "\n" not in parsed["A"]


# ---------------------------------------------------------------------------
# Loading into os.environ
# ---------------------------------------------------------------------------


def test_load_sets_variables(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("ACG_TEST_ALPHA=one\nACG_TEST_BETA=two\n", encoding="utf-8")
    monkeypatch.delenv("ACG_TEST_ALPHA", raising=False)
    monkeypatch.delenv("ACG_TEST_BETA", raising=False)

    applied = load_dotenv(env)

    assert sorted(applied) == ["ACG_TEST_ALPHA", "ACG_TEST_BETA"]
    assert os.environ["ACG_TEST_ALPHA"] == "one"
    monkeypatch.delenv("ACG_TEST_ALPHA")
    monkeypatch.delenv("ACG_TEST_BETA")


def test_a_real_environment_variable_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`PAYMENT_RAIL=razorpay make demo` and CI secrets must beat the file.

    It is also what lets `demo/batch.py --live` set the rail before the file is
    read.
    """
    env = tmp_path / ".env"
    env.write_text("ACG_TEST_GAMMA=from_file\n", encoding="utf-8")
    monkeypatch.setenv("ACG_TEST_GAMMA", "from_environment")

    applied = load_dotenv(env)

    assert applied == [], "nothing was overwritten"
    assert os.environ["ACG_TEST_GAMMA"] == "from_environment"


def test_override_is_available_but_off_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / ".env"
    env.write_text("ACG_TEST_DELTA=from_file\n", encoding="utf-8")
    monkeypatch.setenv("ACG_TEST_DELTA", "from_environment")

    assert load_dotenv(env, override=True) == ["ACG_TEST_DELTA"]
    assert os.environ["ACG_TEST_DELTA"] == "from_file"


def test_a_missing_file_is_not_an_error(tmp_path: Path) -> None:
    """Production injects real environment variables and has no .env."""
    assert load_dotenv(tmp_path / "does-not-exist") == []


def test_a_directory_is_not_an_error(tmp_path: Path) -> None:
    assert load_dotenv(tmp_path) == []


def test_load_returns_names_never_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """So a caller can log "loaded 2 variables" without a secret reaching a log."""
    env = tmp_path / ".env"
    env.write_text("ACG_TEST_SECRET=hunter2\n", encoding="utf-8")
    monkeypatch.delenv("ACG_TEST_SECRET", raising=False)

    applied = load_dotenv(env)

    assert applied == ["ACG_TEST_SECRET"]
    assert "hunter2" not in str(applied)
    monkeypatch.delenv("ACG_TEST_SECRET")


def test_the_shipped_env_example_parses_and_holds_only_placeholders() -> None:
    """The file `make setup` copies must be loadable, and must carry no secret."""
    parsed = parse_env(Path(".env.example").read_text(encoding="utf-8"))

    assert parsed["PAYMENT_RAIL"] == "fake", "the safe rail is the default"
    assert parsed["LLM_PROVIDER"] == "fake", "no model by default"
    assert parsed["RAZORPAY_KEY_SECRET"] == ""
    assert parsed["ANTHROPIC_API_KEY"] == ""
    assert parsed["RAZORPAY_WEBHOOK_SECRET"] == ""
    assert set("x") == set(parsed["RAZORPAY_KEY_ID"].removeprefix("rzp_test_")), (
        "the key id must be an obvious placeholder"
    )


# ---------------------------------------------------------------------------
# Configuration errors, and the live-key guard
# ---------------------------------------------------------------------------


def test_the_default_rail_is_the_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PAYMENT_RAIL", raising=False)
    assert isinstance(build_rail(), FakeRail)


def test_an_unknown_rail_is_a_configuration_error() -> None:
    with pytest.raises(ConfigurationError, match="unknown PAYMENT_RAIL"):
        build_rail("stripe")


def test_missing_razorpay_credentials_name_what_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_abc123")
    monkeypatch.delenv("RAZORPAY_KEY_SECRET", raising=False)

    with pytest.raises(ConfigurationError) as excinfo:
        build_rail("razorpay")

    message = str(excinfo.value)
    assert "RAZORPAY_KEY_SECRET" in message
    assert "RAZORPAY_KEY_ID" not in message.split("Put your")[0], "only the missing one is named"
    assert "docs/RAZORPAY_TESTING.md" in message


def test_a_live_key_is_refused_in_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The single most important guard in the project. A live key spends real money."""
    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_live_realmoney")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "a_real_secret")

    with pytest.raises(ConfigurationError, match="not a test key"):
        build_rail("razorpay")


def test_a_live_key_is_refused_by_the_constructor_too() -> None:
    """Not only via build_rail — constructing the rail directly is refused as well."""
    with pytest.raises(ConfigurationError, match="not a test key"):
        RazorpayRail("rzp_live_realmoney", "secret")


def test_the_live_key_guard_does_not_leak_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The error message must not print the whole credential into a terminal or CI log."""
    with pytest.raises(ConfigurationError) as excinfo:
        RazorpayRail("rzp_live_SUPERSECRETVALUE1234", "secret")
    assert "SUPERSECRET" not in str(excinfo.value)


def test_an_empty_secret_is_refused() -> None:
    with pytest.raises(ConfigurationError, match="RAZORPAY_KEY_SECRET is empty"):
        RazorpayRail("rzp_test_abc", "")
