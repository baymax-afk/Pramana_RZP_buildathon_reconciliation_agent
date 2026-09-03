"""
Ring 2 and Ring 3: the investigator and the orchestrator.

    from recon.agent import Toolbox, EvidenceLedger, investigate, orchestrate

The design constraint, unchanged from `docs/AGENTIC.md`: **the agent may never decide a
match.** Its one lever is to supply evidence the engine did not have and re-run it.
`match_once(evidence=...)` is that lever, `EvidenceProposal` is what fits through it, and
`schemas.py` explains why the field is an enum and why the value is checked against the
shape of every identifier in the batch.
"""

from .investigate import (
    ClaudeInvestigator,
    RecordedInvestigator,
    select as select_investigator,
)
from .ledger import EvidenceLedger
from .orchestrate import AgentRun, Delta, orchestrate
from .schemas import (
    EvidenceField,
    EvidenceProposal,
    EvidenceReceipt,
    InvestigationTrace,
)
from .tools import TOOL_NAMES, TOOL_SPECS, Toolbox

__all__ = [
    "Toolbox", "TOOL_SPECS", "TOOL_NAMES",
    "orchestrate", "AgentRun", "Delta",
    "select_investigator", "ClaudeInvestigator", "RecordedInvestigator",
    "EvidenceLedger", "EvidenceProposal", "EvidenceReceipt", "EvidenceField",
    "InvestigationTrace",
]
