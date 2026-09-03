#!/usr/bin/env python
"""
Repository-root entry point: `python run.py generate | match | sweep | llm-compare`.

The implementation lives in `src/pramana_cli.py` so that it is a REAL packaged module.
It used to sit here at the root, which meant `pip install -e .` produced a `pramana`
console script pointing at a module setuptools had never packaged -- the command existed
and failed. This file is a shim so the documented invocation keeps working while the
installed entry point resolves to the same code.
"""

from pramana_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
