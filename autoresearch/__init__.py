"""Autoresearch v2 - autonomous research engine for ML experimentation.

See design.md for the full design. This package is the engine core: the
event ledger (source of truth), derived state (replay), and materialized
views. The engine core contains no LLM calls.
"""

from autoresearch.ledger import Ledger, LedgerError
from autoresearch.state import CampaignState

__all__ = ["Ledger", "LedgerError", "CampaignState"]
