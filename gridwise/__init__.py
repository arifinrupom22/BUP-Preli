"""GridWise preliminary-round service package.

Pipeline: FastAPI validation -> Gemini interpreter -> deterministic guardrails
-> LP optimizer (HiGHS) -> independent replay validator -> response.
"""
