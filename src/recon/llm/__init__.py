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


def select(disabled: bool = False) -> LLMTier:
    """
    Choose the LLM implementation.

    Order: explicitly disabled -> live Claude if an API key is present -> recorded
    fixtures. The fallback is deterministic and offline, so the tier stays demoable and
    testable without a key; `tier.name` records which one ran, and the metrics block
    prints it, so a recorded run is never mistaken for a live one.
    """
    if disabled:
        from .null import NullTier

        return NullTier()
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from .claude import ClaudeTier

            return ClaudeTier()
        except Exception:
            pass
    from .recorded import RecordedTier

    return RecordedTier()


__all__ = ["select", "LLMTier", "NarrationFields", "ExceptionProse"]
