"""Read-only MCP integration for the DIT Jira service."""

from .client import JiraClient
from .config import Settings

__all__ = ["JiraClient", "Settings"]
__version__ = "0.1.0"

