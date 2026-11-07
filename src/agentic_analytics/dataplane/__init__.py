"""Isolated DuckDB data plane (SEC-02): engine runs in a separate, resource-limited worker."""

from .client import DataPlaneClient, DataPlaneError

__all__ = ["DataPlaneClient", "DataPlaneError"]
