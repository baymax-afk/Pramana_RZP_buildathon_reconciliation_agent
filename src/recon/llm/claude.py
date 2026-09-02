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
    enabled = True

    def __init__(self) -> None:
        import anthropic  # imported lazily so the SDK is optional

        self._client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def _ask(self, prompt: str, max_tokens: int) -> dict:
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
                return {}
            return json.loads(text[start : end + 1])
        except Exception:
            # Degrade to nothing. A tier that raises would make the engine's ability to
            # run without the LLM a lie.
            return {}

    def parse_narration(self, narration: str) -> NarrationFields:
        data = self._ask(_PARSE_PROMPT.format(narration=(narration or "")[:400]), 200)
        return NarrationFields(
            payer_name=_clean(data.get("payer_name")),
            merchant_ref=_clean(data.get("merchant_ref")),
            model=MODEL,
        )

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
