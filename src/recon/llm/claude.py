"""
The live tier -- Claude, via the Anthropic API.

Selected automatically when `ANTHROPIC_API_KEY` is set. It was NOT exercised in this
build: no key was available in the environment, so `RecordedTier` ran instead and the
metrics block reports which one produced the numbers. This code is written to be correct
rather than claimed to be verified.

Every design decision here exists to keep the model inside the trust boundary:

  * It is asked only to EXTRACT, never to match. The prompt says so and the return type
    enforces it -- `NarrationFields` has no field for a payment id or a score.
  * The response is parsed strictly. Anything that is not the expected JSON shape yields
    empty fields rather than a guess, because a malformed response is not evidence.
  * Extracted values are treated as untrusted text: length-capped and stripped of
    control characters before going anywhere near a comparison.
  * Any exception -- network, auth, quota, malformed output -- degrades to empty fields.
    The engine must run to completion with the LLM tier failing, and precision is
    reported both ways precisely so a degraded tier is visible rather than fatal.
"""

from __future__ import annotations

import json
import os
import re

import config as cfg

from .interface import ExceptionProse, NarrationFields

MODEL = "claude-sonnet-5"
MAX_FIELD_LEN = 120

_PARSE_PROMPT = """You are reading one line from an Indian bank statement.

Extract ONLY these two fields and return strict JSON, nothing else:

  {{"payer_name": <the paying company's name, or null>,
    "merchant_ref": <the invoice or reference number they quoted, or null>}}

Rules:
- Return the payer name as it appears, even if the bank truncated it. Do not expand
  abbreviations, do not guess at missing characters, and do not invent a name that is
  not present.
- Normalise a quoted invoice reference to the form INV-YYYY-NNNN when the digits make
  that unambiguous. Otherwise return it as written.
- If a field genuinely is not present, return null for it. A gateway settlement batch
  covering many payers has no single payer name; null is the correct answer there.
- Do NOT identify which payment or invoice this line matches. That is not your task and
  the answer would be discarded.

Narration:
{narration}"""

_EXPLAIN_PROMPT = """A deterministic reconciliation engine has ALREADY decided to refuse
this bank credit. That decision is final and is not yours to revisit.

Write, for a finance operator:
  {{"explanation": <two sentences, plain English, no jargon>,
    "proposed_resolution": <one sentence: the concrete next step>}}

Return strict JSON and nothing else. Do not suggest which payment it should match to.

Category: {category}
Engine's reason: {reason}
Rupees at risk: {rupees:,.2f}"""


def _clean(value) -> str | None:
    """Treat model output as untrusted text: cap length, strip control characters."""
    if not isinstance(value, str):
        return None
    cleaned = re.sub(r"[\x00-\x1f\x7f]", " ", value).strip()
    return cleaned[:MAX_FIELD_LEN] or None


class ClaudeTier:
    name = f"claude:{MODEL}"
    # Instance-level, not class-level: the call cap flips it, and a class attribute
    # would disable the tier for every other instance in the process.
    enabled: bool

    def __init__(self) -> None:
        import anthropic  # imported lazily so the SDK is optional

        from . import load_dotenv

        self.enabled = True
        # `select()` already did this, but constructing the tier directly is a
        # reasonable thing for a caller to do and a KeyError on a scrubbed environment
        # variable is a poor way to find that out. Idempotent, and it never overrides a
        # variable already set.
        load_dotenv()

        # The SDK's defaults are 600s and 2 retries, which on a dead network means a
        # single narration can block for half an hour. Wall clock per call is
        # timeout x (retries + 1), so this bounds one call at ~20s.
        self._client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            timeout=cfg.LLM_TIMEOUT_S,
            max_retries=cfg.LLM_MAX_RETRIES,
        )
        # Narration -> parsed fields. `parse_narration` is a pure function of the
        # narration string, so this cannot change an answer -- only how many times the
        # answer is bought. The matcher re-offers the SAME handful of unreadable
        # narrations on every fixpoint round and again on every permutation pass, so
        # without this the call count is multiplied by MAX_ROUNDS x PERMUTATION_K for
        # no additional information whatsoever.
        self._parse_cache: dict[str, NarrationFields] = {}
        self.calls = 0
        self.failures = 0
        self.cache_hits = 0

    def _ask(self, prompt: str, max_tokens: int) -> dict:
        # A hard ceiling on a live, billed, demo-path dependency. Reaching it disables
        # the tier for the rest of the process rather than continuing to spend: the
        # engine is required to run to completion with the LLM absent, so degrading is
        # always available and is the correct response to a runaway loop.
        if self.calls >= cfg.LLM_MAX_CALLS:
            self.enabled = False
            return {}
        self.calls += 1
        try:
            resp = self._client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            text = "".join(
                block.text for block in resp.content if getattr(block, "type", "") == "text"
            )
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                self.failures += 1
                return {}
            return json.loads(text[start : end + 1])
        except Exception:
            # Degrade to nothing. A tier that raises would make the engine's ability to
            # run without the LLM a lie.
            #
            # But degrading SILENTLY made a different lie possible: a tier that failed
            # on every call was indistinguishable from one that found nothing to add,
            # and the metrics block printed `claude:...` beside numbers no model had
            # touched. The count is surfaced so a degraded run is visible.
            self.failures += 1
            return {}

    def parse_narration(self, narration: str) -> NarrationFields:
        key = narration or ""
        if key in self._parse_cache:
            self.cache_hits += 1
            return self._parse_cache[key]
        data = self._ask(_PARSE_PROMPT.format(narration=key[:400]), 200)
        fields = NarrationFields(
            payer_name=_clean(data.get("payer_name")),
            merchant_ref=_clean(data.get("merchant_ref")),
            model=MODEL,
        )
        self._parse_cache[key] = fields
        return fields

    def stats(self) -> dict[str, int]:
        """What this tier actually did. Reported next to the numbers it influenced."""
        return {
            "calls": self.calls,
            "cache_hits": self.cache_hits,
            "failures": self.failures,
            "capped": int(not self.enabled),
        }

    def explain(self, category: str, reason: str, rupees_at_risk: float) -> ExceptionProse:
        data = self._ask(
            _EXPLAIN_PROMPT.format(
                category=category, reason=reason, rupees=rupees_at_risk
            ),
            400,
        )
        return ExceptionProse(
            explanation=_clean(data.get("explanation")) or reason,
            proposed_resolution=_clean(data.get("proposed_resolution"))
            or "Review and assign manually.",
            model=MODEL,
        )
