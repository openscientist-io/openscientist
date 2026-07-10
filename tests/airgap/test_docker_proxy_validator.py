"""Tests for the air-gap Docker socket proxy's validator sidecar
(``docker/airgap-docker-proxy/validator/validator.py``, RFC section 9 /
issue #218).

Not part of the ``openscientist`` package -- it's a standalone deployment
artifact, loaded here by file path (mirrors how ``docker/agent-entrypoint.py``
is a similarly free-standing script). Covers the actual security logic
(``_reject_reason``, path normalization) as pure-function unit tests, plus
integration-level tests of ``handle_request`` against a mocked backend to
confirm the full request pipeline (path normalization -> exec-deny ->
create-body-validation -> passthrough) behaves correctly end to end.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

_VALIDATOR_PATH = (
    Path(__file__).resolve().parents[2]
    / "docker"
    / "airgap-docker-proxy"
    / "validator"
    / "validator.py"
)


def _load_validator_module():
    spec = importlib.util.spec_from_file_location("airgap_docker_proxy_validator", _VALIDATOR_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


validator = _load_validator_module()


# --------------------------------------------------------- _normalized_path


class TestNormalizedPath:
    def test_unversioned_path_unchanged(self) -> None:
        assert validator._normalized_path("/containers/json") == "/containers/json"

    def test_strips_version_prefix(self) -> None:
        assert validator._normalized_path("/v1.44/containers/json") == "/containers/json"

    def test_strips_two_digit_minor(self) -> None:
        assert validator._normalized_path("/v1.9/containers/create") == "/containers/create"

    def test_only_strips_leading_prefix(self) -> None:
        # A version-looking string elsewhere in the path must not be touched.
        assert (
            validator._normalized_path("/containers/v1.44-container/json")
            == "/containers/v1.44-container/json"
        )


# --------------------------------------------------------- _reject_reason


def _base_body(**host_config_overrides: object) -> dict:
    return {
        "Image": "openscientist-executor:latest",
        "HostConfig": {"NetworkMode": "none", **host_config_overrides},
    }


class TestRejectReasonAcceptsValidBody:
    def test_minimal_valid_body_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())
        assert validator._reject_reason(_base_body()) is None

    def test_image_allowlist_match_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            validator, "_ALLOWED_IMAGES", frozenset({"openscientist-executor:latest"})
        )
        assert validator._reject_reason(_base_body()) is None

    def test_empty_restart_policy_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())
        body = _base_body(RestartPolicy={"Name": "no"})
        assert validator._reject_reason(body) is None


class TestRejectReasonImageAllowlist:
    def test_disallowed_image_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            validator, "_ALLOWED_IMAGES", frozenset({"openscientist-executor:latest"})
        )
        body = {**_base_body(), "Image": "alpine:latest"}
        reason = validator._reject_reason(body)
        assert reason is not None
        assert "Image" in reason

    def test_empty_allowlist_permits_any_image(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Deployments that don't set AIRGAP_DOCKER_PROXY_ALLOWED_IMAGES get
        # no image-name enforcement -- documented behavior, not a silent gap.
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())
        body = {**_base_body(), "Image": "anything:latest"}
        assert validator._reject_reason(body) is None


class TestRejectReasonHostConfigEscapeVectors:
    @pytest.fixture(autouse=True)
    def _no_image_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())

    def test_privileged_rejected(self) -> None:
        assert validator._reject_reason(_base_body(Privileged=True)) is not None

    def test_privileged_false_accepted(self) -> None:
        assert validator._reject_reason(_base_body(Privileged=False)) is None

    @pytest.mark.parametrize(
        "field,value",
        [
            ("Binds", ["/:/host"]),
            ("Mounts", [{"Type": "bind", "Source": "/", "Target": "/host"}]),
            ("CapAdd", ["SYS_ADMIN"]),
            ("Devices", [{"PathOnHost": "/dev/kmsg"}]),
            ("GroupAdd", ["docker"]),
            ("ExtraHosts", ["evil.example:1.2.3.4"]),
            ("SecurityOpt", ["seccomp=unconfined"]),
            ("Sysctls", {"net.ipv4.ip_forward": "1"}),
            ("VolumesFrom", ["some-other-container"]),
            ("Links", ["some-other-container:alias"]),
            ("CgroupParent", "/some/parent"),
        ],
    )
    def test_nonempty_dangerous_field_rejected(self, field: str, value: object) -> None:
        assert validator._reject_reason(_base_body(**{field: value})) is not None

    @pytest.mark.parametrize("field", ["Binds", "Mounts", "CapAdd", "Devices", "SecurityOpt"])
    def test_empty_dangerous_field_accepted(self, field: str) -> None:
        # An empty list/dict for these fields is the SDK's default shape,
        # not an attack -- only non-empty values should trip the deny.
        assert validator._reject_reason(_base_body(**{field: []})) is None

    @pytest.mark.parametrize("field", ["PidMode", "IpcMode", "UTSMode"])
    def test_host_namespace_mode_rejected(self, field: str) -> None:
        assert validator._reject_reason(_base_body(**{field: "host"})) is not None

    @pytest.mark.parametrize("field", ["PidMode", "IpcMode", "UTSMode"])
    def test_host_namespace_mode_case_insensitive(self, field: str) -> None:
        assert validator._reject_reason(_base_body(**{field: "HOST"})) is not None

    @pytest.mark.parametrize("field", ["PidMode", "IpcMode", "UTSMode"])
    def test_non_host_namespace_mode_accepted(self, field: str) -> None:
        assert validator._reject_reason(_base_body(**{field: ""})) is None

    def test_network_mode_bridge_rejected(self) -> None:
        body = {"Image": "openscientist-executor:latest", "HostConfig": {"NetworkMode": "bridge"}}
        assert validator._reject_reason(body) is not None

    def test_network_mode_host_rejected(self) -> None:
        body = {"Image": "openscientist-executor:latest", "HostConfig": {"NetworkMode": "host"}}
        assert validator._reject_reason(body) is not None

    def test_network_mode_missing_rejected(self) -> None:
        body = {"Image": "openscientist-executor:latest", "HostConfig": {}}
        assert validator._reject_reason(body) is not None

    def test_networking_config_endpoints_rejected(self) -> None:
        body = _base_body()
        body["NetworkingConfig"] = {"EndpointsConfig": {"bridge": {}}}
        assert validator._reject_reason(body) is not None

    def test_networking_config_without_endpoints_accepted(self) -> None:
        body = _base_body()
        body["NetworkingConfig"] = {}
        assert validator._reject_reason(body) is None

    def test_restart_policy_always_rejected(self) -> None:
        body = _base_body(RestartPolicy={"Name": "always"})
        assert validator._reject_reason(body) is not None

    def test_host_config_not_a_dict_rejected(self) -> None:
        body = {"Image": "openscientist-executor:latest", "HostConfig": "not-a-dict"}
        assert validator._reject_reason(body) is not None


# --------------------------------------------------------- handle_request (integration)


@pytest.fixture
async def backend_app() -> web.Application:
    """Stand-in for the haproxy layer -- records what it receives and
    returns a canned response, so tests can assert on what actually
    reached "Docker" without needing a real socket."""
    app = web.Application()
    app["received"] = []

    async def echo(request: web.Request) -> web.Response:
        body = await request.read()
        app["received"].append((request.method, request.path, body))
        return web.Response(status=201, text='{"Id":"deadbeef"}')

    app.router.add_route("*", "/{tail:.*}", echo)
    return app


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, backend_app: web.Application
) -> AsyncGenerator[TestClient, None]:
    backend_server = TestServer(backend_app)
    await backend_server.start_server()
    monkeypatch.setattr(
        validator, "_BACKEND_BASE_URL", str(backend_server.make_url("")).rstrip("/")
    )
    monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())

    validator_app = await validator._make_app()
    validator_server = TestServer(validator_app)
    test_client = TestClient(validator_server)
    await test_client.start_server()
    yield test_client
    await test_client.close()
    await backend_server.close()


class TestHandleRequestIntegration:
    # backend_app's mock always answers 201 regardless of route -- these
    # "passthrough" tests assert the validator forwards (didn't 403 it
    # itself), not that it reproduces real Docker's per-route status codes.

    async def test_get_ping_passthrough(self, client: TestClient) -> None:
        resp = await client.get("/_ping")
        assert resp.status == 201

    async def test_networks_reaches_backend_unfiltered(self, client: TestClient) -> None:
        # The validator itself doesn't gate /networks -- that's haproxy's
        # job in the real deployment. This test's mocked backend allows
        # everything, so this confirms the validator doesn't ALSO try to
        # duplicate haproxy's allowlist (single responsibility: only
        # /containers/create body validation and /containers/*/exec deny).
        resp = await client.get("/networks")
        assert resp.status == 201

    async def test_exec_path_denied_regardless_of_backend(self, client: TestClient) -> None:
        resp = await client.post("/containers/abc123/exec")
        assert resp.status == 403

    async def test_exec_path_denied_with_version_prefix(self, client: TestClient) -> None:
        resp = await client.post("/v1.44/containers/abc123/exec")
        assert resp.status == 403

    async def test_exec_start_subpath_denied(self, client: TestClient) -> None:
        # /containers/{id}/exec/{exec_id}/start-shaped paths must not slip
        # through just because they're not an exact match for .../exec.
        resp = await client.post("/containers/abc123/exec/def456/start")
        assert resp.status == 403

    async def test_create_privileged_denied(self, client: TestClient) -> None:
        resp = await client.post(
            "/containers/create",
            json={"Image": "x", "HostConfig": {"NetworkMode": "none", "Privileged": True}},
        )
        assert resp.status == 403

    async def test_create_valid_body_forwarded_to_backend(
        self, client: TestClient, backend_app: web.Application
    ) -> None:
        resp = await client.post(
            "/containers/create",
            json={"Image": "x", "HostConfig": {"NetworkMode": "none"}},
        )
        assert resp.status == 201
        assert len(backend_app["received"]) == 1
        method, path, body = backend_app["received"][0]
        assert method == "POST"
        assert b'"NetworkMode": "none"' in body or b'"NetworkMode":"none"' in body

    async def test_create_invalid_json_rejected(self, client: TestClient) -> None:
        resp = await client.post(
            "/containers/create",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_create_chunked_body_rejected(self, client: TestClient) -> None:
        # The validator's own chunked-rejection reads the Transfer-Encoding
        # header directly (see validator.py's handle_request), so a plain
        # bytes body with that header set exercises the same check without
        # tripping aiohttp client's own chunked auto-detection (which
        # activates on async-generator bodies and conflicts with an
        # explicit Transfer-Encoding header at the client-construction
        # layer, before the request is even sent).
        resp = await client.post(
            "/containers/create",
            data=b'{"Image": "x", "HostConfig": {"NetworkMode": "none"}}',
            headers={"Transfer-Encoding": "chunked"},
        )
        assert resp.status == 400
