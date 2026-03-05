from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from openscientist.api.utils import parse_uuid


def test_parse_uuid_valid_value() -> None:
    value = str(uuid4())

    parsed = parse_uuid(value, "job_id")

    assert isinstance(parsed, UUID)
    assert parsed == UUID(value)


def test_parse_uuid_invalid_value() -> None:
    with pytest.raises(HTTPException) as exc_info:
        parse_uuid("not-a-uuid", "job_id")

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Invalid job_id format"
