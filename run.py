#!/usr/bin/env python
"""
Repository-root entry point: `python run.py generate | match | sweep | llm-compare`.

The implementation lives in `src/pramana_cli.py` so that it is a REAL packaged module.
It used to sit here at the root, which meant `pip install -e .` produced a `pramana`
console script pointing at a module setuptools had never packaged -- the command existed
and failed. This file is a shim so the documented invocation keeps working while the
installed entry point resolves to the same code.

**The one thing this shim has to do besides forwarding.** `pramana_cli` carries a
carefully written guard: if `config` or `recon` cannot be imported it exits with "Pramana
is not installed" and the two `pip install` lines, deliberately rather than repairing
`sys.path` behind the reader's back. That guard could never fire from here, because the
import that fails first on a fresh clone is `pramana_cli` ITSELF -- so the message a new
user actually got was a four-line traceback ending in

    ModuleNotFoundError: No module named 'pramana_cli'

with no indication that one `pip install -e .` fixes it. A good error message in a module
that cannot be imported is not a good error message. So the same guard is repeated here,
where it can run, and it says the same thing for the same reason: this is not a path bug
to paper over, it is an uninstalled package, and the fix is one line the reader can copy.
"""

try:
    from pramana_cli import main
except ModuleNotFoundError as _e:  # pragma: no cover - exercised by tests/test_entry_point.py
    if _e.name not in ("pramana_cli", "config", "loaders", "recon", "scorer"):
        raise
    raise SystemExit(
        f"{_e}\n\n"
        "Pramana is not installed. From the repository root:\n"
        "    pip install -e .            # engine, generator, scorer, CLI\n"
        "    pip install -e '.[api]'     # ...plus the read-only API\n"
        "    pip install -e '.[api,test]'  # ...plus the test suite\n"
        "\n"
        "The engine itself has no dependencies -- the extras are only the API and\n"
        "pytest. Then re-run the same command.\n"
    ) from None

if __name__ == "__main__":
    raise SystemExit(main())
