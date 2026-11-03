from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from openscientist.security import (
    ScannerBlockMiddleware,
    SecurityHeadersMiddleware,
    register_scanner_block_middleware,
    register_security_headers_middleware,
)


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

    with pytest.raises(RuntimeError):
        register_scanner_block_middleware(app)


def _security_headers_middleware_count(app: FastAPI) -> int:
    return sum(
        1 for middleware in app.user_middleware if middleware.cls is SecurityHeadersMiddleware
    )


def _csp_directives(policy: str) -> dict[str, str]:
    directives: dict[str, str] = {}
    for part in policy.split(";"):
        part = part.strip()
        if part:
            name, _, sources = part.partition(" ")
            directives[name] = sources.strip()
    return directives


def test_register_security_headers_middleware_is_idempotent() -> None:
    app = FastAPI()

    first_registration = register_security_headers_middleware(app)
    second_registration = register_security_headers_middleware(app)

    assert first_registration is True
    assert second_registration is True
    assert _security_headers_middleware_count(app) == 1


def _headers_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "true"}

    return app


def test_plain_response_carries_the_security_headers() -> None:
    response = TestClient(_headers_app()).get("/ping")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["permissions-policy"] == (
        "accelerometer=(), camera=(), geolocation=(), gyroscope=(), magnetometer=(), "
        "microphone=(), payment=(), usb=()"
    )


def test_plain_response_carries_the_app_csp() -> None:
    policy = TestClient(_headers_app()).get("/ping").headers["content-security-policy"]
    directives = _csp_directives(policy)

    assert set(directives["script-src"].split()) == {"'self'", "'unsafe-inline'", "'unsafe-eval'"}
    assert set(directives["style-src"].split()) == {"'self'", "'unsafe-inline'"}
    assert set(directives["img-src"].split()) == {"'self'", "data:", "blob:"}
    assert directives["connect-src"] == "'self'"
    assert set(directives["font-src"].split()) == {"'self'", "data:"}
    assert directives["frame-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    assert directives["object-src"] == "'none'"
    assert directives["base-uri"] == "'self'"
    assert directives["form-action"] == "'self'"


def test_a_response_with_its_own_csp_keeps_it() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/sandboxed")
    def sandboxed() -> Response:
        return Response(
            content="x",
            headers={"Content-Security-Policy": "sandbox; connect-src 'none'"},
        )

    response = TestClient(app).get("/sandboxed")

    assert response.headers["content-security-policy"] == "sandbox; connect-src 'none'"
    # The app framing policy must not be layered over a route that owns its CSP.
    assert "x-frame-options" not in response.headers
    assert response.headers["x-content-type-options"] == "nosniff"


def test_an_explicit_referrer_policy_is_kept() -> None:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/review")
    def review() -> Response:
        return Response(status_code=303, headers={"Referrer-Policy": "no-referrer"})

    response = TestClient(app).get("/review")

    assert response.headers["referrer-policy"] == "no-referrer"


@pytest.mark.parametrize(
    ("app_url", "expected_hsts"),
    [
        ("http://localhost:8080", None),
        ("https://openscientist.example", "max-age=31536000; includeSubDomains"),
    ],
)
def test_hsts_follows_the_app_url_scheme(
    monkeypatch: pytest.MonkeyPatch, app_url: str, expected_hsts: str | None
) -> None:
    monkeypatch.setattr(
        "openscientist.settings.get_settings",
        lambda: SimpleNamespace(auth=SimpleNamespace(app_url=app_url)),
    )

    headers = TestClient(_headers_app()).get("/ping").headers

    assert headers.get("strict-transport-security") == expected_hsts
