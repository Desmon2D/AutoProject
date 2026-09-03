"""Read-only MCP adapter for the DIT Git service."""

from .client import GitLabClient
from .config import Settings

__all__ = ["GitLabClient", "Settings"]
__version__ = "0.1.0"

