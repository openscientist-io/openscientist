import pytest
from fastapi import FastAPI

from openscientist.security import ScannerBlockMiddleware, register_scanner_block_middleware


def _scanner_middleware_count(app: FastAPI) -> int:
    return sum(1 for middleware in app.user_middleware if middleware.cls is ScannerBlockMiddleware)


def test_register_scanner_middleware_is_idempotent() -> None:
    app = FastAPI()

    first_registration = register_scanner_block_middleware(app)
    second_registration = register_scanner_block_middleware(app)

    assert first_registration is True
    assert second_registration is True
    assert _scanner_middleware_count(app) == 1


def test_register_scanner_middleware_after_startup_raises() -> None:
    app = FastAPI()
    app.middleware_stack = app.build_middleware_stack()

    with pytest.raises(
        RuntimeError, match="Cannot add middleware after an application has started"
    ):
        register_scanner_block_middleware(app)
