"""Rocket.Chat platform plugin for Hermes Agent."""

try:
    from .adapter import register  # noqa: F401
except Exception:  # pragma: no cover - plugin loader reports adapter errors
    register = None
