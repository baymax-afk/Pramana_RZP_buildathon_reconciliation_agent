"""
The decision recorder: what the engine actually did, captured as it does it.

**This is a transcript, not a narrative.** The engine is deterministic, so its reasoning
IS the computation -- which tiers were tried, which candidates were tested, what the
arithmetic came to in paise, which layer objected. Nothing here is generated after the
fact by a model asked to describe a decision it did not make. That distinction is the
whole point: a plausible story about a match is worth nothing to an auditor, and this
project's argument is that the difference between a plausible answer and a justified one
is the entire problem.

**Recording must not be able to change a verdict.** `match_once` is a pure function of
`ReconInputs`, and MR1 -- the permutation gate that is the project's headline
verification claim -- is only meaningful because of that. So the recorder is optional and
inert: `match_once(inputs)` with no recorder does exactly what it did before, and
`tests/test_explain.py` asserts the assignment map and refusal set hash identically with
recording on and off. If that test ever fails, the explanation is lying about the run it
claims to describe.

**What is captured here is raw.** Turning it into sentences a person can read is
`render.py`'s job, deliberately separated: the recorder must stay cheap and total, and
prose written at capture time would have to be written at every call site by whoever was
editing the matcher that day.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class TierAttempt:
    """One tier's answer for one credit, in one round."""

    tier: str
    outcome: str                     # "assign" | "refuse" | "fell_through" | "conflict"
    category: str = ""               # RefusalCategory value, when it refused
    reason: str = ""                 # the engine's own machine-facing reason
    payment_ids: tuple[str, ...] = ()
    residual_paise: int | None = None
    interval_lo: int | None = None
    interval_hi: int | None = None
    certain_fee: bool | None = None
    uniqueness_margin: float | None = None
    candidates_seen: int = 0


@dataclass(frozen=True, slots=True)
class FieldWeight:
    """One Fellegi-Sunter field comparison, kept with its own detail string."""

    field: str
    level: str                       # "EXACT" | "PARTIAL" | "DISAGREE" | "absent"
    weight: float
    detail: str = ""


@dataclass(slots=True)
class CreditRecord:
    """Everything observed about one bank credit, across every round it survived."""

    bank_txn_id: str
    credit_paise: int
    txn_date: str = ""
    narration: str = ""
    ref_no: str = ""

    round_no: int = 0
    pool_size: int = 0
    window_lo: str = ""
    window_hi: str = ""
    claimed_at_decision: int = 0

    # What the narration parser read, and who read it.
    #
    # `parsed_by` is the string `ParsedNarration` carries -- "regex", or
    # "regex+<model>" when the LLM tier filled a gap. It replaces a `parsed_by_llm`
    # bool that was ALWAYS FALSE: it was assigned from
    # `getattr(parsed, "llm_model", "")`, and `ParsedNarration` has no `llm_model`
    # attribute, so the getattr default swallowed the mistake and the field recorded
    # nothing for as long as it existed. Nothing rendered it, which is why nobody
    # noticed -- and this project reports which tier produced what everywhere else.
    parsed_payer: str | None = None
    parsed_ref: str | None = None
    parsed_txn_count: int | None = None
    parsed_by: str = "regex"

    attempts: list[TierAttempt] = field(default_factory=list)

    # Layer 3
    fs_weight: float | None = None
    fs_prior: float | None = None
    fs_fields: list[FieldWeight] = field(default_factory=list)
    fs_contradicts: bool = False

    # Propose / resolve
    proposed: bool = False
    granted: bool | None = None
    rivals: tuple[str, ...] = ()

    verdict: str = "none"            # "assign" | "refuse" | "none"
    final_payment_ids: tuple[str, ...] = ()
    final_category: str = ""
    final_reason: str = ""

    # Layer 2b: the other credits this one was settled alongside, empty for the ordinary
    # single-credit assignment.
    #
    # The explain layer needs this and cannot infer it. A group member has NO successful
    # tier attempt -- no subset of payments sums to half a settlement, which is the whole
    # reason grouping exists -- so the renderer's "did any attempt win?" test fails and
    # it fell through to *"nothing in the settlement window could account for this
    # credit"*. On a credit the page was simultaneously listing as reconciled.
    group_txn_ids: tuple[str, ...] = ()
    group_credit_paise: int = 0


class Recorder:
    """
    Collects one `CreditRecord` per bank credit.

    **Keyed by transaction, not appended to a list.** A credit that refuses in round 1
    and assigns in round 2 must end up with ONE record describing the decision that
    actually stood, not two contradicting each other -- the fixpoint loop exists
    precisely because an early refusal can be made stale by a later claim, and a
    transcript that showed both without saying which won would be worse than none.
    The record is reset when a credit is re-examined, so what survives is the final
    round's reasoning plus the round number that produced it.
    """

    __slots__ = ("records", "_active")

    def __init__(self) -> None:
        self.records: dict[str, CreditRecord] = {}
        self._active: CreditRecord | None = None

    def begin(self, txn, round_no: int, pool_size: int, claimed: int,
              window: tuple[str, str] = ("", "")) -> CreditRecord:
        rec = CreditRecord(
            bank_txn_id=txn.id,
            credit_paise=txn.credit,
            txn_date=txn.txn_date,
            narration=txn.narration,
            ref_no=txn.ref_no,
            round_no=round_no,
            pool_size=pool_size,
            window_lo=window[0],
            window_hi=window[1],
            claimed_at_decision=claimed,
        )
        self.records[txn.id] = rec
        self._active = rec
        return rec

    @property
    def active(self) -> CreditRecord | None:
        return self._active

    def attempt(self, **kw) -> None:
        if self._active is not None:
            self._active.attempts.append(TierAttempt(**kw))

    def get(self, txn_id: str) -> CreditRecord | None:
        return self.records.get(txn_id)
