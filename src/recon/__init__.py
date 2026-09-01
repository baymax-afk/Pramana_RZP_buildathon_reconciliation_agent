"""
The reconciliation package. Everything under `recon` sits INSIDE the ground-truth
isolation boundary and may never read the generator's answer key.

The boundary is enforced three ways, strongest first:

1. **Function signatures.** Everything under `recon.engine` accepts dataclass objects
   only -- no file paths, no directory handles. `run.py` loads the three sides and
   passes them in. The engine cannot open the truth file because it does not open
   anything.

2. **The audit hook installed below.** Defence against a careless future edit, not the
   primary mechanism.

3. **tests/test_isolation.py**, which deletes the truth directory entirely and asserts
   the engine and all four verification layers still produce identical output.

Note that `recon.generator` is deliberately exempt: the generator WRITES ground truth,
so it must be able to open that path. Only the matching engine and the verification
layers are forbidden from reading it.
"""

from __future__ import annotations

import sys

_TRUTH_MARKER = "_truth"

# Modules permitted to touch the truth directory. The generator writes it; the scorer
# and the external/BenchRec adapters live outside this package entirely.
_ALLOWED_PREFIXES = ("recon.generator",)


def _frame_is_forbidden(module_name: str) -> bool:
    if not (module_name == "recon" or module_name.startswith("recon.")):
        return False
    return not module_name.startswith(_ALLOWED_PREFIXES)


def _truth_guard(event: str, args: tuple) -> None:
    """
    Raise if anything inside recon (other than the generator) opens a path under the
    ground-truth directory.

    This runs on every `open` in the process, so it exits as early as possible: the
    event check and the substring check are both cheap, and the stack walk only
    happens for paths that actually mention the truth directory.
    """
    if event != "open":
        return
    try:
        path = args[0]
    except (IndexError, TypeError):
        return
    if isinstance(path, bytes):
        try:
            path = path.decode("utf-8", "replace")
        except Exception:
            return
    if not isinstance(path, str) or _TRUTH_MARKER not in path:
        return

    frame = sys._getframe(1)
    while frame is not None:
        module_name = frame.f_globals.get("__name__", "")
        if _frame_is_forbidden(module_name):
            raise PermissionError(
                f"Ground-truth isolation violated: module {module_name!r} attempted to "
                f"open {path!r}. Nothing inside recon except recon.generator may read "
                f"the answer key -- see docs/ARCHITECTURE.md, 'The ground-truth "
                f"isolation boundary'."
            )
        frame = frame.f_back


sys.addaudithook(_truth_guard)
