"""
The LLM tier: narration parsing where regex fails, and exception prose after the fact.

`select()` picks an implementation from the environment. The engine only ever holds an
`LLMTier`, so which one is active changes what can be READ and never what can be
DECIDED -- see interface.py for why that is enforced by the return type rather than by
convention.
"""

from __future__ import annotations

import os

from .interface import ExceptionProse, LLMTier, NarrationFields


def select(disabled: bool = False, allow_live: bool = True) -> LLMTier:
    """
    Choose the LLM implementation.

    Order: explicitly disabled -> live Claude if allowed and an API key is present ->
    recorded fixtures. The fallback is deterministic and offline, so the tier stays
    demoable and testable without a key; `tier.name` records which one ran, and the
    metrics block prints it, so a recorded run is never mistaken for a live one.

    **`allow_live=False` means "offline, but not disabled".** Those are different things
    and conflating them was a real bug: when the CLI learned to read `.env`, a key became
    visible and `run.py match` silently started producing `reports/run_output.json` from
    a paid, non-deterministic service -- the artifact the API, the UI and the submission
    all read. Passing `disabled=True` to avoid that would have been wrong in the other
    direction, since it turns the narration tier off entirely and changes the numbers.

    Callers that need a REPRODUCIBLE run and callers that want to MEASURE the live model
    are answering different questions, so they say so separately.
    """
    if disabled:
        from .null import NullTier

        return NullTier()
    if allow_live and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from .claude import ClaudeTier

            return ClaudeTier()
        except Exception:
            pass
    from .recorded import RecordedTier

    return RecordedTier()


__all__ = ["select", "LLMTier", "NarrationFields", "ExceptionProse"]
