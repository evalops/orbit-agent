"""Conformance contract for orbit-agent's agent-facing eval artifacts.

This package is intentionally decoupled from :mod:`orbit_agent` (which eagerly
imports ``dspy``). It depends only on the Python standard library so that the
conformance check is deterministic, LLM-free, and runnable in a minimal CI lane
or as a local command.

See ``contracts/eval-artifacts-v1.md`` for the human-readable design note.
"""

from .contract import CONTRACT_ID, CONTRACT_VERSION

__all__ = ["CONTRACT_ID", "CONTRACT_VERSION"]
