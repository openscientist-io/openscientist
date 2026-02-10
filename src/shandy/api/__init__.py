"""
REST API for SHANDY.

Provides programmatic access to job management and API key management.
All endpoints require API key authentication via Bearer token.

API Key Format:
    Authorization: Bearer <name>:<secret>

Example:
    curl -H "Authorization: Bearer my-key:abc123..." https://shandy.example.com/api/v1/jobs
"""

from .router import api_router

__all__ = ["api_router"]
