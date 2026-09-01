"""
Offline scoring against ground truth.

This package sits OUTSIDE `recon` deliberately. It is the only code permitted to read
`data/generated/_truth/`, and keeping it out of the engine's package means the boundary
is visible in the import graph rather than maintained by discipline.

Data flows one way: engine -> MatchOutput -> scorer. Nothing the scorer computes ever
returns to the engine, and no threshold in `config.py` is set from anything measured
here. If a number in this package could change a matching decision, the project's
central claim -- that its verification works where no ground truth exists -- would be
false.
"""
