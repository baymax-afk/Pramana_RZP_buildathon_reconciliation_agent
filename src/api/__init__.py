"""
The read-only API package.

`api/` had no `__init__.py` and sat at the repository root, so it imported only when the
process happened to be started from there -- `import api.main` from anywhere else failed
outright. It lives under `src/` now, with this file, so that `pip install -e .` makes it
a real importable package and `uvicorn api.main:app` works from any directory.
"""
