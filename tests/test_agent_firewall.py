"""Tests for the air-gapped egress firewall entrypoint.

Rendering tests stub `nft`, `getent`, and `setpriv` on PATH and assert the ruleset
text. Behavioural tests load it in a real container namespace, which is the only way
to catch a rule that renders correctly and never matches: Docker's nat hook rewrites
the embedded resolver's port before this chain runs.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from textwrap import dedent
from typing import Any
from uuid import uuid4

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "docker" / "agent-firewall-entrypoint.sh"
AGENT_DOCKERFILE = REPO_ROOT / "Dockerfile.agent"
TEST_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.firewall-test"
TEST_IMAGE = "openscientist-firewall-test:latest"

DEFAULT_HOSTS = {"postgres": ["172.26.0.2"], "openscientist": ["172.26.0.3"]}
# nft loads the ruleset and setpriv drops privileges.
REQUIRED_PACKAGES = ("nftables", "util-linux")

_POSIX_SHELL_ONLY = pytest.mark.skipif(
    os.name == "nt", reason="requires POSIX shell and executable semantics"
)


class Rendered:
    """What the stubs captured from one run of the entrypoint."""

    def __init__(
        self,
        ruleset: str,
        nft_argv: list[str],
        getent_argv: list[str],
        setpriv_argv: list[str],
        trace: list[str],
    ):
        self.ruleset = ruleset
        self.nft_argv = nft_argv
        self.getent_argv = getent_argv
        self.setpriv_argv = setpriv_argv
        self.trace = trace

    @property
    def rules(self) -> list[str]:
        """The chain body, including its `type ... policy` line."""
        return [
            line.strip()
            for line in self.ruleset.splitlines()
            if line.startswith(" " * 8) and line.strip()
        ]

    @property
    def accepts(self) -> list[str]:
        return [rule for rule in self.rules if rule.startswith("ip daddr")]

    @property
    def command(self) -> list[str]:
        """The argv setpriv was asked to exec, without setpriv's own flags."""
        return [arg for arg in self.setpriv_argv if not arg.startswith("--")]


def _write_stub(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _getent_rows(host: str, ips: list[str]) -> str:
    """Real `getent ahostsv4` repeats each address per socket type and names only the
    first line. `sort -u` in the script exists to collapse that."""
    lines = []
    for index, ip in enumerate(ips):
        lines.append(f"{ip} STREAM {host if index == 0 else ''}".rstrip())
        lines.append(f"{ip} DGRAM")
        lines.append(f"{ip} RAW")
    return "\n".join(lines)


def _render(
    tmp_path: Path,
    allow: str | None,
    hosts: dict[str, list[str]] | None = None,
    *,
    argv: list[str] | None = None,
    shell: list[str] | None = None,
) -> Rendered:
    """Run the entrypoint with its three privileged commands stubbed out.

    `allow=None` leaves the env var unset.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    ruleset_file = tmp_path / "ruleset"
    nft_args = tmp_path / "nft.argv"
    getent_args = tmp_path / "getent.argv"
    setpriv_args = tmp_path / "setpriv.argv"
    # Each stub appends its name here, making call order observable.
    trace = tmp_path / "trace"

    _write_stub(
        bin_dir / "nft",
        f'echo nft >> {trace}\nprintf "%s\\n" "$@" > {nft_args}\ncat > {ruleset_file}\n',
    )
    branches = "\n".join(
        f"    {host}) printf '{_getent_rows(host, ips)}\\n' ;;"
        for host, ips in (DEFAULT_HOSTS if hosts is None else hosts).items()
    )
    _write_stub(
        bin_dir / "getent",
        f"echo getent >> {trace}\n"
        f'printf "%s\\n" "$@" > {getent_args}\ncase "$2" in\n{branches}\n    *) exit 2 ;;\nesac\n',
    )
    _write_stub(
        bin_dir / "setpriv",
        f'echo setpriv >> {trace}\nprintf "%s\\n" "$@" > {setpriv_args}\n',
    )

    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    if allow is not None:
        env["OPENSCIENTIST_FIREWALL_ALLOW"] = allow
    result = subprocess.run(
        [*(shell or ["/bin/sh"]), str(SCRIPT), *(argv or [])],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, f"entrypoint failed: {result.stderr}"

    def lines(path: Path) -> list[str]:
        return path.read_text(encoding="utf-8").splitlines() if path.exists() else []

    return Rendered(
        ruleset_file.read_text(encoding="utf-8") if ruleset_file.exists() else "",
        lines(nft_args),
        lines(getent_args),
        lines(setpriv_args),
        lines(trace),
    )


@_POSIX_SHELL_ONLY
class TestRulesetRendering:
    def test_ruleset_is_exactly_this(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432,openscientist:8082")
        assert rendered.ruleset == dedent(
            """\
            table inet airgap {
                chain output {
                    type filter hook output priority 0; policy drop;
                    oif "lo" accept
                    ct state established,related accept
                    ip daddr 172.26.0.2 tcp dport 5432 accept
                    ip daddr 172.26.0.3 tcp dport 8082 accept
                }
            }
            """
        )

    def test_every_accept_rule_pins_a_destination(self, tmp_path: Path) -> None:
        """An `accept` without an `ip daddr` is reachable to anywhere, whatever port
        it names. Port 53 to any destination was exactly that."""
        rendered = _render(tmp_path, "postgres:5432,openscientist:8082")
        assert [
            rule
            for rule in rendered.rules
            if rule.endswith("accept")
            and not rule.startswith("ip daddr ")
            and rule not in ('oif "lo" accept', "ct state established,related accept")
        ] == []

    def test_no_rule_opens_port_53(self, tmp_path: Path) -> None:
        """The hole: a blanket accept let a job pick its own nameserver and carry data
        out in the query name."""
        rendered = _render(tmp_path, "postgres:5432")
        assert re.search(r"\bdport 53\b", rendered.ruleset) is None

    def test_policy_is_drop(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432")
        assert "policy drop;" in rendered.ruleset

    def test_empty_allowlist_leaves_only_loopback_and_conntrack(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "")
        assert rendered.rules == [
            "type filter hook output priority 0; policy drop;",
            'oif "lo" accept',
            "ct state established,related accept",
        ]

    def test_the_allowlist_env_var_is_optional(self, tmp_path: Path) -> None:
        """`set -u` would abort before the ruleset loads, leaving egress wide open."""
        rendered = _render(tmp_path, None)
        assert "policy drop;" in rendered.ruleset

    def test_a_host_with_several_addresses_gets_a_rule_each(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432", {"postgres": ["172.26.0.2", "172.26.0.9"]})
        assert rendered.accepts == [
            "ip daddr 172.26.0.2 tcp dport 5432 accept",
            "ip daddr 172.26.0.9 tcp dport 5432 accept",
        ]

    def test_the_same_host_on_two_ports_gets_a_rule_each(self, tmp_path: Path) -> None:
        rendered = _render(
            tmp_path, "openscientist:8081,openscientist:8082", {"openscientist": ["172.26.0.3"]}
        )
        assert rendered.accepts == [
            "ip daddr 172.26.0.3 tcp dport 8081 accept",
            "ip daddr 172.26.0.3 tcp dport 8082 accept",
        ]

    def test_an_unresolvable_host_is_skipped(self, tmp_path: Path) -> None:
        """The allowlist is best effort. The drop policy is not."""
        rendered = _render(tmp_path, "postgres:5432,gone.invalid:443")
        assert rendered.accepts == ["ip daddr 172.26.0.2 tcp dport 5432 accept"]

    @pytest.mark.parametrize(
        "allow",
        ["postgres", "postgres:", ":5432", "postgres:http", "postgres:54a32", ",", "a:5432:extra"],
        ids=[
            "no-port",
            "empty-port",
            "empty-host",
            "named-port",
            "non-numeric-port",
            "separators-only",
            "trailing-segment",
        ],
    )
    def test_a_malformed_entry_yields_no_rule(self, tmp_path: Path, allow: str) -> None:
        """A bad entry reaching nft fails the whole load, leaving no firewall at all."""
        rendered = _render(tmp_path, allow)
        assert rendered.accepts == []

    def test_a_malformed_entry_does_not_lose_the_good_ones(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "bogus,postgres:5432,:99,openscientist:8082")
        assert rendered.accepts == [
            "ip daddr 172.26.0.2 tcp dport 5432 accept",
            "ip daddr 172.26.0.3 tcp dport 8082 accept",
        ]

    def test_resolution_asks_for_ipv4_only(self, tmp_path: Path) -> None:
        """Rules are `ip daddr`, so an AAAA address would fail the load."""
        rendered = _render(tmp_path, "postgres:5432")
        assert rendered.getent_argv == ["ahostsv4", "postgres"]

    def test_ruleset_is_loaded_from_stdin(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432")
        assert rendered.nft_argv == ["-f", "-"]


@_POSIX_SHELL_ONLY
class TestPrivilegeDrop:
    def test_root_and_all_capabilities_are_dropped(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432")
        assert [arg for arg in rendered.setpriv_argv if arg.startswith("--")] == [
            "--reuid=agent",
            "--regid=agent",
            "--init-groups",
            "--inh-caps=-all",
            "--bounding-set=-all",
        ]

    def test_the_agent_entrypoint_is_the_default_command(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432")
        assert rendered.command == ["python", "/agent-entrypoint.py"]

    def test_a_command_argument_replaces_the_default(self, tmp_path: Path) -> None:
        """The runner passes none, so the default carries production. Honouring argv is
        what lets a test drive the real script."""
        rendered = _render(tmp_path, "postgres:5432", argv=["python3", "-c", "pass"])
        assert rendered.command == ["python3", "-c", "pass"]

    def test_the_order_is_resolve_then_load_then_drop(self, tmp_path: Path) -> None:
        """Resolution needs the network open, loading needs NET_ADMIN, both precede the
        drop. Any other order yields an empty allowlist or no firewall."""
        rendered = _render(tmp_path, "postgres:5432")
        assert rendered.trace == ["getent", "nft", "setpriv"]


@pytest.mark.parametrize("shell", ["dash", "bash"])
@_POSIX_SHELL_ONLY
def test_the_script_runs_under_posix_shells(tmp_path: Path, shell: str) -> None:
    """The agent image's /bin/sh is dash, so a bashism breaks every air-gapped job."""
    binary = shutil.which(shell)
    if binary is None:
        pytest.skip(f"{shell} not installed")
    rendered = _render(
        tmp_path, "postgres:5432", {"postgres": ["172.26.0.2", "172.26.0.9"]}, shell=[binary]
    )
    assert rendered.accepts == [
        "ip daddr 172.26.0.2 tcp dport 5432 accept",
        "ip daddr 172.26.0.9 tcp dport 5432 accept",
    ]


@pytest.mark.parametrize(
    "dockerfile", [AGENT_DOCKERFILE, TEST_DOCKERFILE], ids=["agent", "stand-in"]
)
def test_the_image_carries_what_the_entrypoint_needs(dockerfile: Path) -> None:
    """Behavioural tests run against the stand-in, so both images have to agree. Reads
    the files, so it runs in CI without Docker."""
    text = dockerfile.read_text(encoding="utf-8")
    for package in REQUIRED_PACKAGES:
        assert package in text, package
    assert any("useradd" in line and "agent" in line for line in text.splitlines())


_HELPERS = dedent(
    """
    import json, os, socket, subprocess

    def tcp(host, port):
        s = socket.socket()
        s.settimeout(2)
        try:
            s.connect((host, port))
            return "open"
        except OSError as exc:
            return type(exc).__name__
        finally:
            s.close()

    target = os.environ["PROBE_TARGET"]
    """
)

_PROBE = _HELPERS + dedent(
    """
    def udp(host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(b"\\x00", (host, port))
            return "open"
        except OSError as exc:
            return type(exc).__name__
        finally:
            s.close()

    caps = ""
    for line in open("/proc/self/status"):
        if line.startswith("CapEff:"):
            caps = line.split()[1]

    try:
        resolved = socket.gethostbyname(target)
    except OSError:
        resolved = None

    flush = subprocess.run(["nft", "flush", "ruleset"], capture_output=True, text=True)

    print(json.dumps({
        "uid": os.getuid(),
        "cap_eff": caps,
        "resolved_target": resolved,
        "tcp_allowlisted": tcp(target, 8000),
        "tcp_other_port_same_host": tcp(target, 9000),
        "udp_public_dns": udp("8.8.8.8", 53),
        "tcp_public_dns": tcp("8.8.8.8", 53),
        "tcp_public_https": tcp("1.1.1.1", 443),
        "nft_flush_rc": flush.returncode,
        "tcp_public_https_after_flush": tcp("1.1.1.1", 443),
    }))
    """
)

_REPLICA_PROBE = _HELPERS + dedent(
    """
    addresses = sorted({i[4][0] for i in socket.getaddrinfo(target, 8000, socket.AF_INET)})
    print(json.dumps({
        "addresses": addresses,
        "reachable": {address: tcp(address, 8000) for address in addresses},
    }))
    """
)


def _wait_listening(container: Any, port: int, timeout: float = 30.0) -> None:
    """Block until the container accepts on the port. The listener binds after start,
    and a probe that raced it saw ConnectionRefused on a slow runner."""
    check = f"import socket; socket.create_connection(('127.0.0.1', {port}), 1).close()"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if container.exec_run(["python3", "-c", check]).exit_code == 0:
            return
        time.sleep(0.5)
    raise AssertionError(f"listener on {port} never came up")


class _Lab:
    """A throwaway bridge network and the containers on it. The entrypoint is
    bind-mounted over the image's copy, so probes exercise the working tree."""

    def __init__(self, client: Any, network: Any, suffix: str) -> None:
        self._client = client
        self.network = network
        self.suffix = suffix
        self.started: list[Any] = []

    def start_listener(self, label: str, ports: list[int], *, alias: str | None = None) -> str:
        """Created then connected, because an alias only attaches before start."""
        name = f"openscientist-firewall-{label}-{self.suffix}"
        serve = " ".join(f"python3 -m http.server {port} --bind 0.0.0.0 &" for port in ports)
        container = self._client.containers.create(
            TEST_IMAGE, entrypoint=["sh", "-c"], command=[f"{serve} wait"], name=name
        )
        self.started.append(container)
        self.network.connect(container, aliases=[alias] if alias else None)
        container.start()
        for port in ports:
            _wait_listening(container, port)
        return name

    def run_probe(self, allow: str, source: str, target: str) -> dict[str, Any]:
        container = self._client.containers.run(
            TEST_IMAGE,
            entrypoint=["/bin/sh", "-c"],
            command=[
                "sed 's/\\r$//' /source-entrypoint.sh > /tmp/entrypoint.sh && "
                "chmod +x /tmp/entrypoint.sh && "
                'exec /tmp/entrypoint.sh "$@"',
                "agent-firewall-entrypoint",
                "python3",
                "-c",
                source,
            ],
            environment={"OPENSCIENTIST_FIREWALL_ALLOW": allow, "PROBE_TARGET": target},
            volumes={str(SCRIPT): {"bind": "/source-entrypoint.sh", "mode": "ro"}},
            network=self.network.name,
            user="root",
            cap_add=["NET_ADMIN"],
            security_opt=["no-new-privileges:true"],
            detach=True,
        )
        self.started.append(container)
        status = container.wait(timeout=120)
        stdout = container.logs(stdout=True, stderr=False).decode()
        stderr = container.logs(stdout=False, stderr=True).decode()
        assert status.get("StatusCode") == 0, f"entrypoint failed: {stderr or stdout}"
        return dict(json.loads(stdout))


@contextmanager
def _lab(client: Any) -> Iterator[_Lab]:
    suffix = uuid4().hex[:12]
    network = client.networks.create(f"openscientist-firewall-net-{suffix}", driver="bridge")
    lab = _Lab(client, network, suffix)
    try:
        yield lab
    finally:
        for container in reversed(lab.started):
            with suppress(Exception):
                container.remove(force=True)
        with suppress(Exception):
            network.remove()


@pytest.fixture(scope="module")
def docker_client() -> Iterator[Any]:
    """`from_env` itself raises without a socket, so it belongs inside the guard:
    outside it, a machine without Docker errors instead of skipping."""
    import docker

    try:
        client = docker.from_env()
        client.ping()
    except Exception as exc:
        pytest.skip(f"Docker not available: {exc}")
    try:
        yield client
    finally:
        client.close()


@pytest.fixture(scope="module")
def firewall_image(docker_client: Any) -> str:
    """Built here rather than required as a prebuilt tag, so these tests run wherever
    Docker does. The tag is stable, so the layer cache survives."""
    import docker

    with TEST_DOCKERFILE.open("rb") as dockerfile:
        try:
            # fileobj without custom_context builds with no context, which is what
            # this Dockerfile wants: it copies nothing.
            docker_client.images.build(fileobj=dockerfile, tag=TEST_IMAGE, rm=True)
        except docker.errors.BuildError as exc:
            log = "\n".join(
                str(entry.get("stream", "")).rstrip()
                for entry in list(exc.build_log)[-20:]
                if isinstance(entry, dict)
            )
            pytest.fail(f"failed to build {TEST_IMAGE}:\n{log}")
    return TEST_IMAGE


@pytest.mark.integration
class TestFirewallBehaviour:
    @pytest.fixture(scope="class")
    def probe(self, docker_client: Any, firewall_image: str) -> Iterator[dict[str, Any]]:
        with _lab(docker_client) as lab:
            # Two listeners, so a blocked connect is not confused with nothing
            # listening on the unallowlisted port.
            target = lab.start_listener("target", [8000, 9000])
            yield lab.run_probe(f"{target}:8000", _PROBE, target)

    def test_service_names_still_resolve(self, probe: dict[str, Any]) -> None:
        """No rule opens port 53. The embedded resolver is reached over lo."""
        assert probe["resolved_target"] is not None

    def test_an_allowlisted_endpoint_is_reachable(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_allowlisted"] == "open"

    def test_another_port_on_an_allowlisted_host_is_blocked(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_other_port_same_host"] != "open"

    def test_a_direct_query_to_a_public_resolver_is_blocked(self, probe: dict[str, Any]) -> None:
        assert probe["udp_public_dns"] == "PermissionError"
        assert probe["tcp_public_dns"] != "open"

    def test_unallowlisted_external_traffic_is_blocked(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_public_https"] != "open"

    def test_the_agent_cannot_undo_the_firewall(self, probe: dict[str, Any]) -> None:
        assert probe["nft_flush_rc"] != 0
        assert probe["tcp_public_https_after_flush"] != "open"

    def test_the_probe_ran_unprivileged(self, probe: dict[str, Any]) -> None:
        assert probe["uid"] != 0
        assert probe["cap_eff"] == "0000000000000000"


@pytest.mark.integration
class TestMultiAddressAllowlist:
    """Splitting resolver output on commas folded a host's addresses into one word,
    which nft rejected as a rule spanning several lines, killing the container before
    the agent started. Vertex and sigv4 Bedrock endpoints answer with eight addresses.
    Two containers sharing an alias reproduce that without external DNS."""

    @pytest.fixture(scope="class")
    def probe(self, docker_client: Any, firewall_image: str) -> Iterator[dict[str, Any]]:
        with _lab(docker_client) as lab:
            alias = f"replica-{uuid4().hex[:8]}"
            lab.start_listener("replica-a", [8000], alias=alias)
            lab.start_listener("replica-b", [8000], alias=alias)
            yield lab.run_probe(f"{alias}:8000", _REPLICA_PROBE, alias)

    def test_the_name_resolves_to_several_addresses(self, probe: dict[str, Any]) -> None:
        """Guards the fixture: with one address the test below passes either way."""
        assert len(probe["addresses"]) >= 2

    def test_every_address_is_reachable(self, probe: dict[str, Any]) -> None:
        assert set(probe["reachable"].values()) == {"open"}
