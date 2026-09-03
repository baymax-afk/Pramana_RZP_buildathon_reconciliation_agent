"""
The LLM on/off comparison -- the measurement W2 has been withholding.

`docs/ARCHITECTURE.md` requires precision to be reported with the LLM tier on and off.
That comparison has never been made, and the reason has been recorded rather than a
number substituted: no API key, and the offline stand-in (`RecordedTier`) applies
essentially the same word-filtering heuristic as `normalize._extract_name`, so it agrees
with the regex tier by construction. Agreement between a parser and a stand-in that
shares the parser's logic is not evidence about what a model would contribute.

This module makes the measurement one command. It also makes the WITHHOLDING one
command, which is the more important half: when the active tier is the stand-in, it
reports the comparison as INVALID and says why, rather than printing two identical
numbers next to each other and letting the reader infer that the LLM changes nothing.

Three things are measured, in increasing order of what they license you to claim:

  1. **Parse yield.** How many narrations the deterministic tier could not read, and how
     many of those the LLM tier filled. Purely mechanical -- valid for any tier.
  2. **Verdict deltas.** Which credits changed verdict between the two arms. This is the
     only thing that can justify a claim about the tier's effect on OUTPUT.
  3. **Precision and match rate, both arms.** The headline the architecture asks for.

Every one of them is reported with the tier name that produced it, so a recorded run can
never be mistaken for a live one.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..engine.normalize import needs_llm, parse
from ..schemas import ReconInputs
from .interface import LLMTier


@dataclass(frozen=True, slots=True)
class ParseYield:
    """What the LLM tier actually added at the FIELD level, before any matching."""

    narrations: int
    unreadable_by_regex: int
    filled_by_llm: int
    names_recovered: int
    refs_recovered: int
    disagreed_with_regex: int

    @property
    def fill_rate(self) -> float:
        return self.filled_by_llm / self.unreadable_by_regex if self.unreadable_by_regex else 0.0


# Tiers whose output is derived from the same rules as the deterministic parser. Their
# agreement with it is a property of shared logic, not evidence about a model.
_STANDIN_TIERS = {"recorded", "null", "disabled"}


def tier_is_measurable(tier: LLMTier) -> tuple[bool, str]:
    """
    Whether a comparison against this tier can support a claim about the LLM's effect.

    A negative answer is not a failure. It is the correct output when the only tier
    available is one that cannot settle the question, and saying so is the whole point
    of the exercise.
    """
    name = getattr(tier, "name", "") or ""
    if not getattr(tier, "enabled", False):
        return False, (
            "the LLM tier is disabled, so there is no second arm to compare against"
        )
    if name.split(":")[0].lower() in _STANDIN_TIERS:
        return False, (
            f"the active tier is {name!r}, an offline stand-in that applies the same "
            "word-filtering heuristic as the regex parser. It agrees with the "
            "deterministic tier BY CONSTRUCTION, so a null result here measures the "
            "stand-in's shared logic and says nothing about what a model would "
            "contribute. Set ANTHROPIC_API_KEY to select the live tier."
        )

    # A tier whose calls did not reach the model measures nothing, and its null result
    # looks exactly like an honest one: both are empty fields. This check exists because
    # the first live key tried against this project was identity-linked, every request
    # 400'd with `anthropic-workspace-id is required`, and the tier degraded silently --
    # so the harness was one run away from publishing "the measured contribution of the
    # LLM is zero" as a finding about Claude rather than about a missing header.
    errors = list(getattr(tier, "transport_errors", ()) or ())
    if errors:
        calls = getattr(tier, "calls_made", 0)
        first = errors[0]
        hint = ""
        if "anthropic-workspace-id" in first:
            hint = (
                " This key is identity-linked: set ANTHROPIC_WORKSPACE_ID to the "
                "workspace it acts in (Console -> Settings -> Workspaces) and re-run."
            )
        return False, (
            f"{len(errors)} of {calls} call(s) to {name!r} never reached the model, so "
            f"every field it returned is a transport failure rather than an answer -- "
            f"and an empty answer is what a SUCCESSFUL call returns for an unreadable "
            f"narration, so the two are indistinguishable in the output. First error: "
            f"{first}.{hint}"
        )
    return True, ""


def measure_parse_yield(inputs: ReconInputs, tier: LLMTier) -> ParseYield:
    """
    What the tier adds at the field level: the mechanical half, valid for any tier.

    Counted over credits only. A debit line has no payer to recover and would dilute
    the denominator with rows the matcher never looks at.
    """
    unreadable = filled = names = refs = disagreed = 0
    narrations = 0

    for txn in inputs.bank_txns:
        if not txn.is_credit:
            continue
        narrations += 1
        base = parse(txn.narration)
        # `needs_llm` is the ENGINE's definition of an unreadable narration, reused
        # rather than restated. It matters here: a settlement batch legitimately carries
        # no payer name, because it covers many payers, so counting "no name" as a gap
        # would inflate the denominator with rows where the absence is the right answer
        # and the tier is deliberately never consulted.
        if not needs_llm(base):
            continue
        unreadable += 1

        got = tier.parse_narration(txn.narration)
        added_name = bool(got.payer_name) and not base.payer_name
        added_ref = bool(got.merchant_ref) and not base.merchant_ref
        if added_name:
            names += 1
        if added_ref:
            refs += 1
        if added_name or added_ref:
            filled += 1
        # How often the tier reads a field DIFFERENTLY from the regex tier.
        #
        # Informational, and deliberately not an alarm. It counts what the tier
        # RETURNS, not what the engine uses: `parse_with_llm` fills gaps only, so a
        # disagreement here is discarded by the merge and cannot reach a verdict.
        # Verified -- on a jammed narration the regex tier reads "ORCHIDFOODSPVT" and
        # the stand-in reads "ORCHIDFOODS PVT"; the merged parse keeps the regex value.
        #
        # This counter's note used to read "must be 0", which was simply wrong about its
        # own metric: a non-zero value says the two parsers disagree about a hard string,
        # which is expected and harmless. The boundary is enforced in `parse_with_llm`
        # and asserted directly in tests/test_llm_tier.py.
        if (
            base.payer_name
            and got.payer_name
            and base.payer_name.strip().casefold() != got.payer_name.strip().casefold()
        ):
            disagreed += 1

    return ParseYield(
        narrations=narrations,
        unreadable_by_regex=unreadable,
        filled_by_llm=filled,
        names_recovered=names,
        refs_recovered=refs,
        disagreed_with_regex=disagreed,
    )


def _verdict_map(out) -> dict[str, str]:
    """bank_txn_id -> a verdict string comparable across arms."""
    verdicts = {}
    for a in out.assignments:
        verdicts[a.bank_txn_id] = "assign:" + ",".join(sorted(a.payment_ids))
    for r in out.refusals:
        verdicts[r.bank_txn_id] = f"refuse:{r.category.value}"
    for txn_id in out.no_candidate:
        verdicts[txn_id] = "no_candidate"
    return verdicts


def _outcome(verdict: str) -> str:
    """assign / refuse / no_candidate / absent -- the DECISION, without its reason."""
    return verdict.split(":", 1)[0]


def split_changes(
    changes: tuple[tuple[str, str, str], ...],
) -> tuple[tuple[tuple[str, str, str], ...], tuple[tuple[str, str, str], ...]]:
    """
    Separate changed DECISIONS from changed REASONS.

    The distinction is not pedantry, and measuring it is why this harness exists. A
    credit that moves `refuse:decomposition_out_of_bounds` -> `refuse:unexplained_residual`
    has the same verdict, the same money in the same place, and the same effect on
    precision and match rate. What changed is the sentence the operator reads.

    That is a real contribution and a much weaker one than moving a verdict, and the two
    must not be reported under one heading. Calling a reason change a "verdict change"
    is how a tier that improves explanations gets credited with improving decisions.
    """
    outcome, reason = [], []
    for txn_id, off, on in changes:
        (outcome if _outcome(off) != _outcome(on) else reason).append((txn_id, off, on))
    return tuple(outcome), tuple(reason)


def diff_verdicts(out_on, out_off) -> tuple[tuple[str, str, str], ...]:
    """
    Every credit whose verdict differs between the arms, as (txn_id, off, on).

    This is the only measurement that can justify a claim about the tier's effect on
    output. Parse yield can be large while this is empty -- recovering a payer name the
    matcher did not need changes nothing -- and reporting the first as though it implied
    the second would be exactly the overclaim this project exists to argue against.
    """
    on, off = _verdict_map(out_on), _verdict_map(out_off)
    changes = []
    for txn_id in sorted(set(on) | set(off)):
        a, b = off.get(txn_id, "absent"), on.get(txn_id, "absent")
        if a != b:
            changes.append((txn_id, a, b))
    return tuple(changes)
