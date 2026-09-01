"""
The data generator.

This package is the ONLY thing inside `recon` permitted to write the ground-truth
directory (see `recon/__init__.py`). It writes the answer key; the matching engine and
the verification layers never read it.

The generator also keeps its own EXACT fee schedule, deliberately not shared with
`recon.engine.fees`, which knows only a rate band. If both used the same function,
MR4 (conservation) would be tautological: the engine would reconcile because it was
inverting the very function that produced the data.
"""
