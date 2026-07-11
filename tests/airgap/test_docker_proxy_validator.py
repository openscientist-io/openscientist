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

    def test_legitimate_output_and_data_binds_accepted(self) -> None:
        # The actual shape container_manager.py's _build_volumes() produces:
        # one rw output mount, one ro data-file mount, both under ordinary
        # job-scoped paths -- must NOT be rejected (this was the original
        # bug: blanket-denying any Binds broke every real request).
        body = _base_body(
            Binds=[
                "/Users/dev/shandy/jobs/abc123/output:/output:rw",
                "/Users/dev/shandy/jobs/abc123/data:/data:ro",
            ]
        )
        assert validator._reject_reason(body) is None


class TestRejectReasonBindTraversalBypass:
    """Adversarial review (2026-07-10) found _reject_bind_reason's original
    bare rstrip("/") let dot-dot traversal strings evade the denylist while
    still resolving to the real forbidden path once the kernel processes
    the mount. These pin the fix (posixpath.normpath before comparison)."""

    @pytest.fixture(autouse=True)
    def _no_image_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())

    def test_dotdot_traversal_to_docker_socket_rejected(self) -> None:
        body = _base_body(Binds=["/tmp/../var/run/docker.sock:/var/run/docker.sock:rw"])
        assert validator._reject_reason(body) is not None

    def test_dotdot_traversal_to_ssh_keys_rejected(self) -> None:
        body = _base_body(Binds=["/tmp/../root/.ssh:/stolen:ro"])
        assert validator._reject_reason(body) is not None

    def test_doubled_leading_slash_rejected(self) -> None:
        body = _base_body(Binds=["//etc:/stolen:ro"])
        assert validator._reject_reason(body) is not None

    def test_macos_private_alias_of_docker_socket_rejected(self) -> None:
        body = _base_body(Binds=["/private/var/run/docker.sock:/var/run/docker.sock:rw"])
        assert validator._reject_reason(body) is not None

    def test_macos_private_etc_rejected(self) -> None:
        body = _base_body(Binds=["/private/etc:/stolen:ro"])
        assert validator._reject_reason(body) is not None

    def test_normal_looking_job_path_still_accepted(self) -> None:
        # Traversal-hardening must not start false-positive-rejecting
        # ordinary paths that happen to contain no traversal at all.
        body = _base_body(Binds=["/Users/dev/shandy/jobs/abc123/output:/output:rw"])
        assert validator._reject_reason(body) is None


class TestRejectReasonVolumeDriverDeviceBypass:
    """Adversarial review (2026-07-10) found HostConfig.Mounts[].Source for
    a "Type": "volume" mount is a volume NAME, not a path -- _bind_sources
    originally only read .Source, so the local volume driver's bind-mount
    escape hatch (VolumeOptions.DriverConfig.Options.device) sailed through
    completely unchecked. This was the most severe finding: a single
    request mounting the entire host root, no string tricks needed."""

    @pytest.fixture(autouse=True)
    def _no_image_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())

    def test_local_driver_device_bind_to_host_root_rejected(self) -> None:
        body = _base_body(
            Mounts=[
                {
                    "Type": "volume",
                    "Source": "harmless-looking-name",
                    "Target": "/host_root",
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "local",
                            "Options": {"type": "none", "o": "bind", "device": "/"},
                        }
                    },
                }
            ]
        )
        assert validator._reject_reason(body) is not None

    def test_local_driver_device_bind_to_docker_socket_dir_rejected(self) -> None:
        body = _base_body(
            Mounts=[
                {
                    "Type": "volume",
                    "Source": "another-harmless-name",
                    "Target": "/x",
                    "VolumeOptions": {
                        "DriverConfig": {
                            "Name": "local",
                            "Options": {"type": "none", "o": "bind", "device": "/var/run"},
                        }
                    },
                }
            ]
        )
        assert validator._reject_reason(body) is not None

    def test_volume_mount_without_driver_device_still_checked_by_name_field(self) -> None:
        # Sanity: a genuine named-volume mount (no bind trick) has no
        # meaningful "Source" path to reject -- shouldn't false-positive.
        body = _base_body(
            Mounts=[{"Type": "volume", "Source": "some-real-volume-name", "Target": "/x"}]
        )
        assert validator._reject_reason(body) is None


class TestRejectReasonSecurityOptAllowlist:
    """Adversarial review (2026-07-10) found the original blanket-deny on
    any non-empty SecurityOpt broke every real request (container_manager.py
    sets security_opt=["no-new-privileges:true"], a hardening flag). A
    substring-denylist draft was also rejected on review: Docker's
    seccomp/apparmor options accept an arbitrary custom profile (path or
    inline JSON) that can be maximally permissive without containing the
    literal word "unconfined" anywhere -- no denylist can be complete for
    that in principle. Landed as an allowlist of the one legitimate value
    instead."""

    @pytest.fixture(autouse=True)
    def _no_image_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())

    def test_no_new_privileges_true_accepted(self) -> None:
        body = _base_body(SecurityOpt=["no-new-privileges:true"])
        assert validator._reject_reason(body) is None

    def test_empty_security_opt_accepted(self) -> None:
        body = _base_body(SecurityOpt=[])
        assert validator._reject_reason(body) is None

    def test_missing_security_opt_accepted(self) -> None:
        assert validator._reject_reason(_base_body()) is None

    def test_seccomp_unconfined_rejected(self) -> None:
        body = _base_body(SecurityOpt=["seccomp=unconfined"])
        assert validator._reject_reason(body) is not None

    def test_apparmor_unconfined_rejected(self) -> None:
        body = _base_body(SecurityOpt=["apparmor=unconfined"])
        assert validator._reject_reason(body) is not None

    def test_seccomp_custom_profile_path_rejected(self) -> None:
        # Not literally "unconfined" -- a custom profile can be equally
        # permissive. The allowlist rejects it precisely because it isn't
        # the one known-safe value, without needing to evaluate the
        # profile's actual content.
        body = _base_body(SecurityOpt=["seccomp=/tmp/maximally-permissive.json"])
        assert validator._reject_reason(body) is not None

    def test_selinux_label_type_spc_t_rejected(self) -> None:
        # SELinux "super-privileged container" type -- full confinement
        # bypass on SELinux-enforcing hosts. Real Docker syntax, not
        # covered by a naive "unconfined"/"disable" substring check.
        body = _base_body(SecurityOpt=["label=type:spc_t"])
        assert validator._reject_reason(body) is not None

    def test_no_new_privileges_false_rejected(self) -> None:
        # Explicitly re-enabling privilege escalation is not the one
        # allowed value, even though it superficially resembles it.
        body = _base_body(SecurityOpt=["no-new-privileges:false"])
        assert validator._reject_reason(body) is not None

    def test_multiple_entries_all_must_be_allowed(self) -> None:
        body = _base_body(SecurityOpt=["no-new-privileges:true", "seccomp=unconfined"])
        assert validator._reject_reason(body) is not None

    def test_non_list_security_opt_rejected(self) -> None:
        body = _base_body(SecurityOpt="no-new-privileges:true")
        assert validator._reject_reason(body) is not None

    def test_non_string_entry_rejected(self) -> None:
        body = _base_body(SecurityOpt=[123])
        assert validator._reject_reason(body) is not None


class TestRejectReasonCaseFoldingBypass:
    """Docker's Go daemon matches JSON keys to struct fields
    case-insensitively (last matching key wins); this validator's checks
    use exact-case dict lookups. A body can carry a validator-safe,
    correctly-cased field alongside a differently-cased, dangerous
    duplicate that this validator never inspects but dockerd itself would
    honor -- defeating every check in _reject_reason at once."""

    @pytest.fixture(autouse=True)
    def _no_image_allowlist(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(validator, "_ALLOWED_IMAGES", frozenset())

    def test_duplicate_hostconfig_different_case_rejected(self) -> None:
        # Safe "HostConfig" is what this validator checks; a sibling
        # "hostconfig" carrying Privileged=True would be what dockerd
        # actually honors (last matching key wins in Go's decoder).
        body = _base_body()
        body["hostconfig"] = {"NetworkMode": "none", "Privileged": True}
        assert validator._reject_reason(body) is not None

    def test_duplicate_image_different_case_rejected(self) -> None:
        body = _base_body()
        body["image"] = "not-the-executor-image:latest"
        assert validator._reject_reason(body) is not None

    def test_duplicate_privileged_within_hostconfig_rejected(self) -> None:
        body = _base_body()
        body["HostConfig"]["privileged"] = True
        assert validator._reject_reason(body) is not None

    def test_lone_wrong_case_privileged_rejected_with_no_decoy(self) -> None:
        # No "Privileged" (correct case) present at all -- just a single
        # "privileged" key. Not a case-duplicate (nothing to collide with),
        # but Go's decoder still case-insensitively matches a lone
        # differently-cased key to the struct field with no exact-case
        # sibling required. A naive "reject only on duplicates" fix would
        # miss this; field lookups must themselves be case-insensitive.
        body = _base_body(privileged=True)
        assert validator._reject_reason(body) is not None

    def test_lone_wrong_case_binds_docker_socket_rejected(self) -> None:
        body = _base_body(binds=["/var/run/docker.sock:/var/run/docker.sock"])
        assert validator._reject_reason(body) is not None

    def test_lone_wrong_case_image_field_checked_against_allowlist(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            validator, "_ALLOWED_IMAGES", frozenset({"openscientist-executor:latest"})
        )
        body = {"image": "not-the-executor-image:latest", "HostConfig": {"NetworkMode": "none"}}
        reason = validator._reject_reason(body)
        assert reason is not None
        assert "not in the allowed executor image list" in reason

    def test_duplicate_networkmode_different_case_rejected(self) -> None:
        body = _base_body()
        body["HostConfig"]["networkmode"] = "bridge"
        assert validator._reject_reason(body) is not None

    def test_case_duplicate_in_nested_mount_dict_rejected(self) -> None:
        body = _base_body(Mounts=[{"Source": "/data", "Target": "/data", "source": "/etc"}])
        assert validator._reject_reason(body) is not None

    def test_case_duplicate_inside_list_element_rejected(self) -> None:
        body = _base_body()
        body["HostConfig"]["RestartPolicy"] = {"Name": "no", "name": "always"}
        assert validator._reject_reason(body) is not None

    def test_no_case_duplicates_accepted(self) -> None:
        # Sanity check: a normal body with no case collisions anywhere
        # still passes -- the new check isn't overly aggressive.
        body = _base_body(SecurityOpt=["no-new-privileges:true"])
        assert validator._reject_reason(body) is None

    def test_same_case_repeated_key_not_flagged(self) -> None:
        # Exact-case duplicate keys in a Python dict literal are impossible
        # (the second assignment just overwrites the first), so this
        # exercises the same-case path directly against the helper rather
        # than via _base_body to confirm it doesn't misfire on ordinary
        # single-key dicts.
        assert validator._find_case_duplicate_key({"HostConfig": {}}) is None


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
