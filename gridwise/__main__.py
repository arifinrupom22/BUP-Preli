"""Entrypoint: python -m gridwise

Binds 0.0.0.0 on $PORT (default 8000) so the service is reachable from
outside the container/host as required by the judging harness.
"""
from __future__ import annotations

import os

import uvicorn

from gridwise.config import get_settings


def main() -> None:
    settings = get_settings()
    port = int(os.getenv("PORT", str(settings.port)))
    uvicorn.run("gridwise.app:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
