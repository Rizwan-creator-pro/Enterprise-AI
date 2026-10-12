"""
Enterprise AI Hub - Agent Query Engine Compatibility Shim
==========================================================
The core implementation is housed within the agent/ package.
This module re-exports all symbols from agent.query_engine for
backwards compatibility.
"""
from agent.query_engine import *
import agent.query_engine as _qe

__all__ = [name for name in dir(_qe) if not name.startswith("_")]
