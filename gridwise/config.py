"""Central configuration.

Reads configuration from environment variables only. No secret values are
hard-coded anywhere in this repository; see .env.example for the documented
variable names.

A minimal .env loader is built in (no external dependency): KEY=VALUE lines
from a .env file at the project root are applied ONLY for variables not
already present in the real environment, so the process environment always
wins. Values are never logged or exposed.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def load_dotenv_if_present() -> None:
    """Populate os.environ from .env without overriding real variables.

    Silently skips blank lines, comments, and malformed lines. Never prints,
    logs, or returns secret values. Idempotent.
    """
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.is_file():
        return
    try:
        raw = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key or not value:
            continue
        os.environ.setdefault(key, value)


load_dotenv_if_present()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    # Interpreter selection: "api" (Gemini) or "mock" (offline tests only).
    interpreter: str = field(default_factory=lambda: os.getenv("GRIDWISE_INTERPRETER", "api").strip().lower())
    gemini_api_key: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", "").strip())
    # Default verified live: gemini-3.5-flash-lite (fast, separate free-tier
    # quota pool, structured-output capable). Override with GEMINI_MODEL.
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip())

    llm_max_attempts: int = field(default_factory=lambda: max(1, _get_int("GRIDWISE_LLM_MAX_ATTEMPTS", 3)))
    llm_timeout_s: float = field(default_factory=lambda: max(1.0, _get_float("GRIDWISE_LLM_TIMEOUT_S", 20.0)))

    cache_size: int = field(default_factory=lambda: max(0, _get_int("GRIDWISE_CACHE_SIZE", 2048)))
    cache_ttl_s: float = field(default_factory=lambda: max(0.0, _get_float("GRIDWISE_CACHE_TTL_S", 3600.0)))

    # Allowed browser origin for CORS. Empty string -> localhost dev origins.
    frontend_origin: str = field(default_factory=lambda: os.getenv("FRONTEND_ORIGIN", "").strip().rstrip("/"))

    port: int = field(default_factory=lambda: _get_int("PORT", 8000))


def get_settings() -> Settings:
    return Settings()


def service_ready() -> bool:
    """Health readiness: the interpreter layer is importable and configured.

    The mock interpreter needs no key. The Gemini interpreter only needs a key
    at request time; the service is ready to serve /health regardless, so a
    missing key never makes the deployment fail its readiness probe.
    """
    return True
