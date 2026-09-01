"""
The verification layers.

These run INSIDE the engine's package and therefore inside the ground-truth isolation
boundary: every check here works on the engine's own inputs and outputs, with no access
to the answer key. That is the whole point. A check that needs to know the right answer
is useless on a merchant's own books, which is the only place this system would ever
actually run.
"""
