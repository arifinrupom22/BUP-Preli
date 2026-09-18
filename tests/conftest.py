"""Shared pytest fixtures.

All API tests run with GRIDWISE_INTERPRETER=mock so the suite is deterministic
and offline. The real Gemini interpreter is exercised in a separate opt-in
test gated on GEMINI_API_KEY presence.
"""
from __future__ import annotations

import os
import sys

import pytest

# Must be set before gridwise.app imports.
os.environ.setdefault("GRIDWISE_INTERPRETER", "mock")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

