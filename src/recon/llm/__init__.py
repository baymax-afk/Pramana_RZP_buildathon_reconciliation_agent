"""
The LLM tier: narration parsing where regex fails, and exception prose after the fact.

`select()` picks an implementation from the environment. The engine only ever holds an
`LLMTier`, so which one is active changes what can be READ and never what can be
DECIDED -- see interface.py for why that is enforced by the return type rather than by
convention.
"""

from __future__ import annotations

import os
from pathlib import Path

from .interface import ExceptionProse, LLMTier, NarrationFields

# The repo root -- resolved relative to this file rather than through `config`, so
# importing the LLM package never drags the whole config module in behind it.
_DOTENV = Path(__file__).resolve().parents[3] / ".env"


def load_dotenv(path: Path | None = None) -> tuple[str, ...]:
    """
    Populate `os.environ` from the repository's `.env`, and report what it set.

    **Why this exists rather than `python-dotenv`.** Three lines of parsing against a
    file this project already gitignores is not worth a dependency, and a dependency
    here would be one more thing to install before the engine runs.

    **It never overrides a variable that is already set.** A real environment variable
    is a deliberate act by whoever launched the process; a file on disk is a default.
    Silently shadowing the former with the latter is how a run ends up using a
    credential nobody in the room believes it is using.

    Blank lines, `#` comments, a leading `export `, and surrounding quotes are all
    tolerated, because every one of them appears in `.env` files people actually write.
    A malformed line is skipped rather than raised on: this is a convenience loader, and
    failing to start the engine over a stray line in an optional file is the wrong
    trade.

    Returns the names it set -- never the values -- so a caller can report that a
    credential was loaded without putting it in a log.
    """
    path = path or _DOTENV
    if not path.is_file():
        return ()
    loaded: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return tuple(loaded)


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
    if allow_live:
        # A key in `.env` and a key in the environment must select the same tier.
        # Without this, whether the live tier ran depended on how the process happened
        # to be launched -- and the metrics block would print `recorded` next to the
        # numbers with nothing about the run looking wrong. Gated on `allow_live`: a
        # caller that explicitly declined the live tier gets no reason to have `.env`
        # read on its behalf either.
        load_dotenv()
    if allow_live and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from .claude import ClaudeTier

            return ClaudeTier()
        except Exception:
            pass
    from .recorded import RecordedTier

    return RecordedTier()


__all__ = ["select", "load_dotenv", "LLMTier", "NarrationFields", "ExceptionProse"]
