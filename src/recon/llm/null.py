"""
The disabled tier.

Exists so `--no-llm` is a real code path rather than a branch that skips calls. The
engine runs to completion against this, and `docs/METRICS.md` requires precision to be
reported both ways -- if the LLM tier made precision worse, that is what the metrics
block would say.
"""

from __future__ import annotations

from .interface import ExceptionProse, NarrationFields


class NullTier:
    name = "disabled"
    enabled = False

    def parse_narration(self, narration: str) -> NarrationFields:
        """Extracts nothing. A narration the regex tier failed on simply stays unparsed."""
        return NarrationFields(model="disabled", note="LLM tier disabled")

    def explain(self, category: str, reason: str, rupees_at_risk: float) -> ExceptionProse:
        """
        The deterministic reason string, unadorned.

        Note this still produces a usable exception: the engine's own `reason` already
        says what happened and why, because it was written to be read by a human. The
        LLM improves the phrasing; it was never load-bearing for comprehension.
        """
        return ExceptionProse(
            explanation=reason,
            proposed_resolution="Review the candidates and assign manually.",
            model="disabled",
        )
