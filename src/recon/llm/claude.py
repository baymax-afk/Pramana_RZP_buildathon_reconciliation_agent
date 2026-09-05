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

**Degrading silently is not the same as degrading safely, and this tier used to do the
first.** Every failure returned `{}`, which is exactly what a successful call returns
for an unreadable narration -- so a tier whose calls were ALL failing was indistinguishable
from a tier that had honestly found nothing, and `llm-compare` would have reported "the
measured contribution is zero" as though it were a finding about the model.

That is not hypothetical. The first live key tried against this code was identity-linked
and every request 400'd with `anthropic-workspace-id is required`; the tier swallowed all
of them and reported empty fields. The comparison harness would have published a false
zero attributed to Claude.

So failures are still non-fatal -- the engine must run to completion with the tier
broken -- but they are now COUNTED and NAMED. `ClaudeTier.transport_errors` holds what
went wrong, and any consumer reporting a contribution figure is expected to check it
first.
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

        # An identity-linked key must name the workspace it acts in, or EVERY request
        # 400s. Sent as a default header rather than per-call so no code path can forget
        # it, and omitted entirely when unset so an ordinary key is unaffected.
        headers = {}
        workspace = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
        if workspace:
            headers["anthropic-workspace-id"] = workspace

        # The SDK's defaults are 600s and 2 retries, which on a dead network means a
        # single narration can block for half an hour. Wall clock per call is
        # timeout x (retries + 1), so this bounds one call at ~20s.
        self._client = anthropic.Anthropic(
            api_key=os.environ["ANTHROPIC_API_KEY"],
            default_headers=headers or None,
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
        # Two failure lists, because they mean opposite things about a measurement.
        #
        # A TRANSPORT failure means the request never reached the model, so the empty
        # fields it produced are not the model's answer and must not be counted as one.
        # A PARSE failure means the model answered and the answer was unusable -- which
        # IS a fact about the model, and belongs in the measurement rather than
        # invalidating it.
        #
        # Collapsing the two would make one malformed JSON body in an otherwise clean
        # run of 127 calls invalidate the whole comparison, and report the reason as
        # "the requests never reached the model" -- a false claim, in the guard written
        # to stop false claims.
        self.transport_errors: list[str] = []
        self.parse_failures: list[str] = []
        self.calls_made = 0
        self.cache_hits = 0

    def _ask(self, prompt: str, max_tokens: int) -> dict:
        # A hard ceiling on a live, billed, demo-path dependency. Reaching it disables
        # the tier for the rest of the process rather than continuing to spend: the
        # engine is required to run to completion with the LLM absent, so degrading is
        # always available and is the correct response to a runaway loop.
        if self.calls_made >= cfg.LLM_MAX_CALLS:
            self.enabled = False
            return {}
        self.calls_made += 1

        # Scoped tightly to the network call: only what happens BEFORE a response
        # exists can be a transport failure.
        try:
            resp = self._client.messages.create(
                model=MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as e:
            # Degrade to nothing, but RECORD it. A tier that raises would make the
            # engine's ability to run without the LLM a lie; a tier that fails silently
            # makes every measurement of its contribution a lie instead.
            self.transport_errors.append(f"{type(e).__name__}: {str(e)[:200]}")
            return {}

        # The model answered. Anything wrong from here on is about the answer, not the
        # pipe, so it degrades to empty fields without impugning the run.
        try:
            text = "".join(
                block.text
                for block in resp.content
                if getattr(block, "type", "") == "text"
            )
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                self.parse_failures.append("no JSON object in response")
                return {}
            return json.loads(text[start : end + 1])
        except Exception as e:
            # Degrade to nothing. A tier that raises would make the engine's ability to
            # run without the LLM a lie.
            #
            # But degrading SILENTLY made a different lie possible: a tier that failed
            # on every call was indistinguishable from one that found nothing to add,
            # and the metrics block printed `claude:...` beside numbers no model had
            # touched. Named and counted so a degraded run is visible.
            self.parse_failures.append(f"{type(e).__name__}: {str(e)[:200]}")
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
        """
        What this tier actually did. Reported next to the numbers it influenced.

        `failures` is the SUM of transport and parse failures -- the two are kept apart
        internally because they mean different things about a measurement (see the
        class docstring and `recon.llm.compare.tier_is_measurable`, which reads
        `transport_errors` on its own), but a single "how much went wrong" figure is
        what belongs beside a headline number.
        """
        return {
            "calls": self.calls_made,
            "cache_hits": self.cache_hits,
            "failures": len(self.transport_errors) + len(self.parse_failures),
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
