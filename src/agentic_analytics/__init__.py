"""Agentic Analytics: an MCP-native analytical runtime."""

from .ids import PROTOCOL_VERSION
from .support import check_supported_platform

# Fail fast on native Windows, before any submodule that needs POSIX is imported (C14).
check_supported_platform()

__all__ = ["PROTOCOL_VERSION"]
__version__ = "0.1.0a0"
