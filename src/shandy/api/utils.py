"""Shared API helper utilities."""

from uuid import UUID

from fastapi import HTTPException, status


def parse_uuid(value: str, field_name: str) -> UUID:
    """Parse a UUID string and raise an HTTP 400 for invalid values."""
    try:
        return UUID(value)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid {field_name} format",
        ) from e
