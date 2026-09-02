"""Load config/policy.yaml into a validated, frozen object.

Rupees become integer paise exactly once — here — so no other module ever has to
wonder which unit it is holding. If the YAML is malformed the process fails at
startup rather than at the moment someone tries to spend money.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from ap2_min.models import inr

DEFAULT_POLICY_PATH = Path("config/policy.yaml")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StandingAuthorisationPolicy(_Frozen):
    """The bounds a simulated user signs into their open mandates at startup."""

    daily_budget: int = Field(..., ge=0, description="paise")
    per_txn_min: int = Field(..., ge=0, description="paise")
    per_txn_max: int = Field(..., ge=0, description="paise")
    allowed_payees: list[str] = Field(..., min_length=1)
    validity_hours: int = Field(..., gt=0)
    ship_to_pincode: str

    @model_validator(mode="after")
    def _sane(self) -> StandingAuthorisationPolicy:
        if self.per_txn_min > self.per_txn_max:
            raise ValueError("per_txn_min must not exceed per_txn_max")
        if self.per_txn_max > self.daily_budget:
            raise ValueError(
                "a per-transaction cap above the daily budget is not a cap — "
                "one purchase could exhaust the day"
            )
        return self


class MandatePolicy(_Frozen):
    checkout_ttl_seconds: int = Field(..., gt=0)
    payment_ttl_seconds: int = Field(..., gt=0)
    clock_skew_seconds: int = Field(..., ge=0)


class RecoveryPolicy(_Frozen):
    max_attempts: int = Field(..., ge=1, le=10)
    method_fallback: list[str] = Field(..., min_length=1)
    backoff_base_seconds: float = Field(..., ge=0)
    backoff_factor: float = Field(..., ge=1)
    backoff_max_seconds: float = Field(..., ge=0)

    def backoff_for(self, retry_index: int) -> float:
        """Seconds to wait before retry ``retry_index`` (0-based), capped.

        Retry 0 is the *second* attempt overall — the first attempt is never
        delayed. So retry 0 waits ``backoff_base_seconds``, retry 1 waits
        ``base * factor``, and so on up to ``backoff_max_seconds``.
        """
        if self.backoff_base_seconds <= 0:
            return 0.0
        return min(
            self.backoff_base_seconds * (self.backoff_factor**retry_index),
            self.backoff_max_seconds,
        )


class CircuitBreakerPolicy(_Frozen):
    failure_threshold: int = Field(..., ge=1)
    reset_after_seconds: float = Field(..., gt=0)


class LlmPolicy(_Frozen):
    """Where a model may run, and where it may not.

    ``forbidden`` is not decoration. tests/test_failure_modes.py greps the money
    path modules for LLM imports, and the acceptance checklist does the same.
    """

    allowed: list[str] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)


class Policy(_Frozen):
    standing_authorisation: StandingAuthorisationPolicy
    mandates: MandatePolicy
    recovery: RecoveryPolicy
    circuit_breaker: CircuitBreakerPolicy
    llm: LlmPolicy

    @model_validator(mode="after")
    def _recovery_fits_methods(self) -> Policy:
        if self.recovery.max_attempts > len(self.recovery.method_fallback):
            raise ValueError(
                "recovery.max_attempts exceeds the number of fallback methods; "
                "the playbook would have nowhere left to go"
            )
        return self


def _to_paise(raw: dict[str, Any]) -> dict[str, Any]:
    """Rename the ``*_inr`` keys to paise fields, converting as we go."""
    standing = dict(raw["standing_authorisation"])
    converted = {
        "daily_budget": inr(standing.pop("daily_budget_inr")),
        "per_txn_min": inr(standing.pop("per_txn_min_inr")),
        "per_txn_max": inr(standing.pop("per_txn_max_inr")),
        **standing,
    }
    return {**raw, "standing_authorisation": converted}


def load_policy(path: str | Path | None = None) -> Policy:
    """Read and validate the policy file.

    Resolution order: explicit argument, then ``$POLICY_FILE``, then the default
    ``config/policy.yaml``.
    """
    resolved = Path(path or os.environ.get("POLICY_FILE") or DEFAULT_POLICY_PATH)
    if not resolved.exists():
        raise FileNotFoundError(
            f"policy file not found at {resolved}. Copy config/policy.yaml or set $POLICY_FILE."
        )
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{resolved} does not contain a YAML mapping")
    return Policy.model_validate(_to_paise(raw))
