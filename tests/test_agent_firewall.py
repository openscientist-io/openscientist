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
import sysconfig
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from textwrap import dedent
from typing import Any
from uuid import uuid4

import pytest

from tests.launcher import EXECUTOR_IMAGE, Launcher, executor_image_problem

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "docker" / "agent-firewall-entrypoint.sh"
AGENT_DOCKERFILE = REPO_ROOT / "Dockerfile.agent"
EXECUTOR_DOCKERFILE = REPO_ROOT / "Dockerfile.executor"
TEST_DOCKERFILE = REPO_ROOT / "docker" / "Dockerfile.firewall-test"
TEST_IMAGE = "openscientist-firewall-test:latest"

DEFAULT_HOSTS = {"postgres": ["172.26.0.2"], "openscientist": ["172.26.0.3"]}
# nft loads the ruleset and setpriv drops privileges.
REQUIRED_PACKAGES = ("nftables", "util-linux")


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
        return [
            rule for rule in self.rules if rule.startswith("ip daddr") and rule.endswith("accept")
        ]

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


def _default_gateway() -> str | None:
    """The gateway the entrypoint pins from /proc/net/route's little-endian
    hex word, so a rendering test and the script read the same address."""
    for line in Path("/proc/net/route").read_text(encoding="ascii").splitlines()[1:]:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000":
            word = fields[2]
            return ".".join(str(int(word[i : i + 2], 16)) for i in (6, 4, 2, 0))
    return None


def _resolv_nameservers() -> list[str]:
    """The nameservers the entrypoint pins from /etc/resolv.conf, deduped and
    sorted the way the script does."""
    return sorted(
        {
            line.split()[1]
            for line in Path("/etc/resolv.conf").read_text(encoding="ascii").splitlines()
            if line.split() and line.split()[0] == "nameserver"
        }
    )


def _render(
    tmp_path: Path,
    allow: str | None,
    hosts: dict[str, list[str]] | None = None,
    *,
    argv: list[str] | None = None,
    shell: list[str] | None = None,
    mode: str | None = None,
    run_as: str | None = None,
) -> Rendered:
    """Run the entrypoint with privileged commands stubbed out."""
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
    if mode is not None:
        env["OPENSCIENTIST_FIREWALL_MODE"] = mode
    if run_as is not None:
        env["OPENSCIENTIST_FIREWALL_RUN_AS"] = run_as
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


class TestRulesetRendering:
    def test_ruleset_is_exactly_this(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432,openscientist:8082")
        assert rendered.ruleset == dedent(
            """\
            table inet airgap {
                chain output {
                    type filter hook output priority 0; policy drop;
                    ip daddr 127.0.0.11 drop
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
            "ip daddr 127.0.0.11 drop",
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


class TestRestrictedRulesetRendering:
    """Restrict mode keeps public egress open and drops the cloud metadata,
    link-local, private and host-gateway ranges, with the allowlisted web
    services open in front of the drops."""

    def test_restrict_ruleset_is_exactly_this(self, tmp_path: Path) -> None:
        gateway = _default_gateway()
        rendered = _render(
            tmp_path, "postgres:5432,openscientist:8081,openscientist:8082", mode="restrict"
        )
        expected = [
            "table inet airgap {",
            "    chain output {",
            "        type filter hook output priority 0; policy accept;",
            '        oif "lo" accept',
            "        ct state established,related accept",
            "        ip daddr 172.26.0.2 tcp dport 5432 accept",
            "        ip daddr 172.26.0.3 tcp dport 8081 accept",
            "        ip daddr 172.26.0.3 tcp dport 8082 accept",
            *[
                f"        {family} daddr {server} {proto} dport 53 accept"
                for server in _resolv_nameservers()
                for family, proto in (
                    ("ip6" if ":" in server else "ip", "udp"),
                    ("ip6" if ":" in server else "ip", "tcp"),
                )
            ],
            "        ip daddr 169.254.0.0/16 drop",
            "        ip daddr 10.0.0.0/8 drop",
            "        ip daddr 172.16.0.0/12 drop",
            "        ip daddr 192.168.0.0/16 drop",
            "        ip daddr 100.64.0.0/10 drop",
            "        ip6 daddr fe80::/10 drop",
            "        ip6 daddr fc00::/7 drop",
            "        ip6 daddr fd00:ec2::254 drop",
        ]
        if gateway is not None:
            expected.append(f"        ip daddr {gateway} drop")
        expected += ["    }", "}"]
        assert rendered.ruleset.splitlines() == expected

    def test_restrict_policy_is_accept(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432", mode="restrict")
        assert "policy accept;" in rendered.ruleset

    def test_restrict_drops_every_blocked_range(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432", mode="restrict")
        for rule in (
            "ip daddr 169.254.0.0/16 drop",
            "ip daddr 10.0.0.0/8 drop",
            "ip daddr 172.16.0.0/12 drop",
            "ip daddr 192.168.0.0/16 drop",
            "ip daddr 100.64.0.0/10 drop",
            "ip6 daddr fe80::/10 drop",
            "ip6 daddr fc00::/7 drop",
            "ip6 daddr fd00:ec2::254 drop",
        ):
            assert rule in rendered.rules

    def test_restrict_exceptions_open_before_the_drops(self, tmp_path: Path) -> None:
        """A web service on the bridge is reachable only while its accept rule
        precedes the range drop that covers the bridge itself."""
        rendered = _render(tmp_path, "postgres:5432", mode="restrict")
        rules = rendered.rules
        first_drop = rules.index("ip daddr 169.254.0.0/16 drop")
        assert [
            rules.index(rule)
            for rule in rules
            if rule.startswith("ip daddr ") and rule.endswith("accept")
        ] and all(
            rules.index(rule) < first_drop
            for rule in rules
            if rule.startswith("ip daddr ") and rule.endswith("accept")
        )

    def test_restrict_pins_the_default_route_gateway(self, tmp_path: Path) -> None:
        gateway = _default_gateway()
        if gateway is None:
            pytest.skip("no default route on this host")
        rendered = _render(tmp_path, "postgres:5432", mode="restrict")
        assert f"ip daddr {gateway} drop" in rendered.rules

    def test_restrict_without_a_default_route_omits_the_gateway_rule(self, tmp_path: Path) -> None:
        if _default_gateway() is not None:
            pytest.skip("this host has a default route")
        rendered = _render(tmp_path, "postgres:5432", mode="restrict")
        # Every IPv4 drop is a range; a single pinned gateway address would not be.
        ip4_drops = [
            rule for rule in rendered.rules if rule.startswith("ip daddr") and rule.endswith("drop")
        ]
        assert all("/" in rule.split()[2] for rule in ip4_drops)

    def test_restricted_run_user_is_dropped_to(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432", mode="restrict", run_as="executor")
        assert [arg for arg in rendered.setpriv_argv if arg.startswith(("--reuid", "--regid"))] == [
            "--reuid=executor",
            "--regid=jobs",
        ]

    def test_unset_mode_keeps_the_airgap_policy(self, tmp_path: Path) -> None:
        """The flag is opt-in, so a launch that predates it keeps the drop policy."""
        rendered = _render(tmp_path, "postgres:5432")
        assert "policy drop;" in rendered.ruleset


@pytest.mark.parametrize("shell", ["dash", "bash"])
def test_restrict_mode_runs_under_posix_shells(tmp_path: Path, shell: str) -> None:
    """The agent image's /bin/sh is dash, so a bashism breaks every restricted launch."""
    binary = shutil.which(shell)
    if binary is None:
        pytest.skip(f"{shell} not installed")
    rendered = _render(tmp_path, "postgres:5432", mode="restrict", shell=[binary])
    assert "policy accept;" in rendered.ruleset
    assert "ip daddr 169.254.0.0/16 drop" in rendered.rules


class TestPrivilegeDrop:
    def test_root_and_all_capabilities_are_dropped(self, tmp_path: Path) -> None:
        rendered = _render(tmp_path, "postgres:5432")
        assert [arg for arg in rendered.setpriv_argv if arg.startswith("--")] == [
            "--reuid=agent",
            "--regid=jobs",
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
    "dockerfile,run_as,carries_entrypoint",
    [
        (AGENT_DOCKERFILE, "agent", True),
        (TEST_DOCKERFILE, "agent", False),
        (EXECUTOR_DOCKERFILE, "executor", True),
    ],
    ids=["agent", "stand-in", "executor"],
)
def test_the_image_carries_what_the_entrypoint_needs(
    dockerfile: Path, run_as: str, carries_entrypoint: bool
) -> None:
    """Behavioural tests run against the stand-in, so both images have to agree. The
    stand-in bind-mounts the entrypoint at run time, so it alone may omit it.
    Reads the files, so it runs in CI without Docker."""
    text = dockerfile.read_text(encoding="utf-8")
    for package in REQUIRED_PACKAGES:
        assert package in text, package
    assert any("useradd" in line and run_as in line for line in text.splitlines())
    if carries_entrypoint:
        assert "agent-firewall-entrypoint.sh" in text


_HELPERS = dedent(
    """
    import json, os, socket, subprocess

    def tcp(host, port):
        s = socket.socket()
        # Five seconds, because a public handshake can be slower than two on a
        # loaded host and a flaky timeout reads as a missing allowlist.
        s.settimeout(5)
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

_RESTRICT_PROBE = _HELPERS + dedent(
    """
    import subprocess

    def default_gateway():
        for line in open("/proc/net/route").read().splitlines()[1:]:
            fields = line.split()
            if len(fields) > 2 and fields[1] == "00000000":
                word = fields[2]
                return ".".join(str(int(word[i:i + 2], 16)) for i in (6, 4, 2, 0))
        return None

    caps = ""
    for line in open("/proc/self/status"):
        if line.startswith("CapEff:"):
            caps = line.split()[1]

    try:
        resolved = socket.gethostbyname(target)
    except OSError:
        resolved = None

    gateway = default_gateway()
    meta = os.environ.get("PROBE_METADATA_TARGET")
    flush = subprocess.run(["nft", "flush", "ruleset"], capture_output=True, text=True)

    print(json.dumps({
        "uid": os.getuid(),
        "cap_eff": caps,
        "resolved_target": resolved,
        "gateway": gateway,
        "tcp_proxy": tcp(target, 8000),
        "tcp_broker": tcp(target, 8001),
        "tcp_postgres": tcp(target, 8002),
        "tcp_unallowlisted_port": tcp(target, 9000),
        "tcp_metadata": tcp("169.254.169.254", 80),
        "tcp_metadata_listener": tcp(meta, 8000) if meta else None,
        "tcp_gateway": tcp(gateway, 9999) if gateway else None,
        "tcp_public_https": tcp("1.1.1.1", 443),
        "nft_flush_rc": flush.returncode,
        "tcp_public_https_after_flush": tcp("1.1.1.1", 443),
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
        #: Removed after the containers, since a network with a container on it cannot go.
        self.extra_networks: list[Any] = []

    def start_listener(
        self,
        label: str,
        ports: list[int],
        *,
        alias: str | None = None,
        network: Any | None = None,
    ) -> str:
        """Created then connected, because an alias only attaches before start."""
        name = f"openscientist-firewall-{label}-{self.suffix}"
        serve = " ".join(f"python3 -m http.server {port} --bind 0.0.0.0 &" for port in ports)
        container = self._client.containers.create(
            TEST_IMAGE, entrypoint=["sh", "-c"], command=[f"{serve} wait"], name=name
        )
        self.started.append(container)
        (network or self.network).connect(container, aliases=[alias] if alias else None)
        container.start()
        for port in ports:
            _wait_listening(container, port)
        return name

    def address_of(self, name: str, network: Any) -> str:
        """The container's IP on the named network, for probes that target by IP."""
        container = self._client.containers.get(name)
        nets = container.attrs["NetworkSettings"]["Networks"]
        return str(
            next(
                v["IPAddress"]
                for v in nets.values()
                if v.get("NetworkID", "").startswith(network.id[:12])
            )
        )

    def run_launcher_probe(
        self,
        allow: str,
        source: str,
        target: str,
        *,
        read_only: bool,
        extra_environment: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Run the launcher and agent in the same Docker DNS namespace."""
        launcher_source = dedent(
            """
            import json, os
            from openscientist_launcher import api
            from openscientist_launcher.config import LauncherConfig

            template = api.agent_run_kwargs
            def probe_template(*args, **kwargs):
                result = template(*args, **kwargs)
                result.update(json.loads(os.environ["PROBE_KWARGS"]))
                result["environment"].update(json.loads(os.environ["PROBE_ENV"]))
                return result
            api.agent_run_kwargs = probe_template
            server = api.Server(LauncherConfig.from_env())
            print(json.dumps(server.create_agent(json.loads(os.environ["PROBE_REQUEST"]))))
            """
        )
        with TemporaryDirectory(prefix="firewall-launcher-") as temporary:
            root = Path(temporary)
            job_id = str(uuid4())
            (root / job_id / "data").mkdir(parents=True)
            digest = str(self._client.images.get(TEST_IMAGE).attrs["Id"])
            environment = {
                "PYTHONPATH": "/test-src:/test-deps",
                "JOBS_ROOT": str(root),
                "AGENT_IMAGE": TEST_IMAGE,
                "EXECUTOR_IMAGE": TEST_IMAGE,
                "AGENT_IMAGE_DIGESTS": digest,
                "EXECUTOR_IMAGE_DIGESTS": digest,
                "LAUNCHER_INSTANCE_ID": self.suffix,
                "LAUNCHER_NETWORK": self.network.name,
                "AGENT_MAX_MEMORY_MIB": "512",
                "EXECUTOR_MAX_MEMORY_MIB": "512",
                "PROBE_ENV": json.dumps({"PROBE_TARGET": target, **(extra_environment or {})}),
                "AGENT_MAX_CPU": "1",
                "EXECUTOR_MAX_CPU": "1",
                "MAX_LIFETIME_SECONDS": "120",
                "PROBE_REQUEST": json.dumps(
                    {
                        "job_id": job_id,
                        "mode": "discovery",
                        "lifetime_seconds": 120,
                        "memory_mib": 512,
                        "cpu": 1,
                        "airgapped": True,
                        "egress_allow": allow.split(","),
                    }
                ),
                "PROBE_KWARGS": json.dumps(
                    {
                        "read_only": read_only,
                        "command": ["python3", "-c", source.replace("__TARGET__", repr(target))],
                        "volumes": {
                            str(SCRIPT): {"bind": "/agent-firewall-entrypoint.sh", "mode": "ro"}
                        },
                    }
                ),
            }
            launcher = self._client.containers.create(
                TEST_IMAGE,
                entrypoint=["python3", "-c", launcher_source],
                environment=environment,
                network=self.network.name,
                volumes={
                    "/var/run/docker.sock": {"bind": "/var/run/docker.sock", "mode": "rw"},
                    str(REPO_ROOT / "src"): {"bind": "/test-src", "mode": "ro"},
                    sysconfig.get_path("purelib"): {"bind": "/test-deps", "mode": "ro"},
                    str(root): {"bind": str(root), "mode": "rw"},
                },
            )
            self.started.append(launcher)
            launcher.start()
            status = launcher.wait(timeout=120)
            logs = launcher.logs().decode()
            assert status["StatusCode"] == 0, logs
            container = self._client.containers.get(json.loads(logs)["id"])
            self.started.append(container)
            status = container.wait(timeout=120)
            stdout = container.logs(stdout=True, stderr=False).decode()
            stderr = container.logs(stdout=False, stderr=True).decode()
            assert status["StatusCode"] == 0, stderr or stdout
            probe = dict(json.loads(stdout))
            probe["extra_hosts"] = container.attrs["HostConfig"]["ExtraHosts"]
            probe["read_only"] = container.attrs["HostConfig"]["ReadonlyRootfs"]
            container.remove(force=True)
            return probe

    def run_probe(
        self,
        allow: str,
        source: str,
        target: str,
        *,
        mode: str,
        extra_environment: dict[str, str] | None = None,
        extra_networks: list[Any] | None = None,
        resolv_conf: Path | None = None,
    ) -> dict[str, Any]:
        """The first network connected is the default route; the extras add
        direct routes for the link-local listeners. `resolv_conf` bind-mounts
        over /etc/resolv.conf; Docker leaves a mounted file untouched."""
        if mode == "airgap":
            assert extra_networks is None and resolv_conf is None
            return self.run_launcher_probe(
                allow, source, target, read_only=False, extra_environment=extra_environment
            )
        environment = {
            "OPENSCIENTIST_FIREWALL_ALLOW": allow,
            "OPENSCIENTIST_FIREWALL_MODE": mode,
            "PROBE_TARGET": target,
        }
        if extra_environment:
            environment.update(extra_environment)
        volumes: dict[str, dict[str, str]] = {
            str(SCRIPT): {"bind": "/agent-firewall-entrypoint.sh", "mode": "ro"}
        }
        if resolv_conf is not None:
            volumes[str(resolv_conf)] = {"bind": "/etc/resolv.conf", "mode": "ro"}
        container = self._client.containers.create(
            TEST_IMAGE,
            entrypoint=["/agent-firewall-entrypoint.sh"],
            command=["python3", "-c", source],
            environment=environment,
            volumes=volumes,
            user="root",
            cap_add=["NET_ADMIN"],
            security_opt=["no-new-privileges:true"],
        )
        self.started.append(container)
        self.network.connect(container)
        for network in extra_networks or []:
            network.connect(container)
        container.start()
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
        for extra in lab.extra_networks:
            with suppress(Exception):
                extra.remove()


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
@pytest.mark.parametrize("transport", ["tcp", "udp"])
def test_airgap_blocks_embedded_resolver(
    docker_client: Any, firewall_image: str, transport: str
) -> None:
    source = _HELPERS + dedent(
        """
        import struct

        query = struct.pack("!6H", 20299, 256, 1, 0, 0, 0)
        query += b"".join(bytes([len(part)]) + part.encode() for part in target.split("."))
        query += b"\\x00\\x00\\x01\\x00\\x01"
        transport = os.environ["PROBE_TRANSPORT"]
        kind = socket.SOCK_STREAM if transport == "tcp" else socket.SOCK_DGRAM
        with socket.socket(socket.AF_INET, kind) as resolver:
            resolver.settimeout(2)
            try:
                resolver.connect(("127.0.0.11", 53))
                if transport == "tcp":
                    query = struct.pack("!H", len(query)) + query
                resolver.sendall(query)
                response = resolver.recv(4096)
                reached = bool(response)
            except OSError:
                reached = False
        print(json.dumps({"resolver_reachable": reached}))
        """
    )
    with _lab(docker_client) as lab:
        target = lab.start_listener("dns-target", [8000])
        probe = lab.run_probe(
            f"{target}:8000",
            source,
            target,
            mode="airgap",
            extra_environment={"PROBE_TRANSPORT": transport},
        )
    assert probe["resolver_reachable"] is False


@pytest.mark.integration
@pytest.mark.parametrize("read_only", [True, False], ids=["readonly", "writable"])
def test_launcher_pins_airgap_hosts(
    docker_client: Any, firewall_image: str, read_only: bool
) -> None:
    source = dedent(
        """
        import json, os, socket, struct
        from pathlib import Path

        target = __TARGET__
        addresses = sorted({r[4][0] for r in socket.getaddrinfo(target, 8000, socket.AF_INET)})
        reachable = []
        for address in addresses:
            with socket.create_connection((address, 8000), timeout=2):
                reachable.append(address)
        query = struct.pack("!6H", 20299, 256, 1, 0, 0, 0)
        query += b"\\x07example\\x03com\\x00\\x00\\x01\\x00\\x01"
        responses = []
        for host in ("127.0.0.11", "8.8.8.8"):
            for kind in (socket.SOCK_STREAM, socket.SOCK_DGRAM):
                with socket.socket(socket.AF_INET, kind) as resolver:
                    resolver.settimeout(1)
                    try:
                        resolver.connect((host, 53))
                        message = struct.pack("!H", len(query)) + query
                        resolver.sendall(message if kind == socket.SOCK_STREAM else query)
                        responses.append(bool(resolver.recv(4096)))
                    except OSError:
                        responses.append(False)
        print(json.dumps({
            "addresses": addresses,
            "reachable": reachable,
            "hosts": Path("/etc/hosts").read_text(),
            "dns_responses": responses,
            "uid": os.getuid(),
        }))
        """
    )
    with _lab(docker_client) as lab:
        alias = f"pinned-{lab.suffix}"
        names = [
            lab.start_listener("pin-a", [8000], alias=alias),
            lab.start_listener("pin-b", [8000], alias=alias),
        ]
        addresses = sorted(lab.address_of(name, lab.network) for name in names)
        probe = lab.run_launcher_probe(f"{alias}:8000", source, alias, read_only=read_only)
    assert probe["read_only"] is read_only
    assert probe["addresses"] == addresses
    assert probe["reachable"] == addresses
    assert probe["dns_responses"] == [False] * 4
    assert probe["uid"] != 0
    for address in addresses:
        assert f"{alias}:{address}" in probe["extra_hosts"]
        assert any(line.split() == [address, alias] for line in probe["hosts"].splitlines())


@pytest.mark.integration
class TestFirewallBehaviour:
    @pytest.fixture(scope="class")
    def probe(self, docker_client: Any, firewall_image: str) -> Iterator[dict[str, Any]]:
        with _lab(docker_client) as lab:
            # Two listeners, so a blocked connect is not confused with nothing
            # listening on the unallowlisted port.
            target = lab.start_listener("target", [8000, 9000])
            yield lab.run_probe(
                f"{target}:8000",
                _PROBE,
                target,
                mode="airgap",
                extra_environment={"RES_OPTIONS": "attempts:1 timeout:1"},
            )

    def test_service_names_still_resolve(self, probe: dict[str, Any]) -> None:
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
            yield lab.run_probe(f"{alias}:8000", _REPLICA_PROBE, alias, mode="airgap")

    def test_the_name_resolves_to_several_addresses(self, probe: dict[str, Any]) -> None:
        assert len(probe["addresses"]) >= 2

    def test_every_address_is_reachable(self, probe: dict[str, Any]) -> None:
        assert set(probe["reachable"].values()) == {"open"}


@pytest.mark.integration
class TestRestrictedFirewallBehaviour:
    """Restrict mode keeps public egress but refuses the metadata, link-local,
    private and host-gateway ranges. A live listener inside each refused range
    proves the rule fires, not that the address happens to be down."""

    @pytest.fixture(scope="class")
    def probe(self, docker_client: Any, firewall_image: str) -> Iterator[dict[str, Any]]:
        with _lab(docker_client) as lab:
            # The web services (proxy, broker, Postgres) on the private bridge,
            # plus a fourth port on the same host that stays off the allowlist.
            target = lab.start_listener("svc", [8000, 8001, 8002, 9000])
            meta_network = docker_client.networks.create(
                f"openscientist-firewall-ll-{lab.suffix}",
                driver="bridge",
                ipam={"config": [{"subnet": "169.254.0.0/16", "gateway": "169.254.0.1"}]},
            )
            lab.extra_networks.append(meta_network)
            meta = lab.start_listener("meta", [8000], network=meta_network)
            yield lab.run_probe(
                f"{target}:8000,{target}:8001,{target}:8002",
                _RESTRICT_PROBE,
                target,
                mode="restrict",
                extra_environment={"PROBE_METADATA_TARGET": lab.address_of(meta, meta_network)},
                extra_networks=[meta_network],
            )

    def test_service_names_still_resolve(self, probe: dict[str, Any]) -> None:
        assert probe["resolved_target"] is not None

    def test_the_web_services_stay_reachable(self, probe: dict[str, Any]) -> None:
        """The allowlist keeps the proxy, the broker and Postgres open although
        they sit on a private bridge address."""
        assert probe["tcp_proxy"] == "open"
        assert probe["tcp_broker"] == "open"
        assert probe["tcp_postgres"] == "open"

    def test_an_unallowlisted_port_on_a_private_host_is_blocked(
        self, probe: dict[str, Any]
    ) -> None:
        assert probe["tcp_unallowlisted_port"] == "TimeoutError"

    def test_the_metadata_endpoint_is_unreachable(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_metadata"] == "TimeoutError"

    def test_a_link_local_listener_is_unreachable(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_metadata_listener"] == "TimeoutError"

    def test_the_host_gateway_is_unreachable(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_gateway"] == "TimeoutError"

    def test_public_internet_stays_reachable(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_public_https"] == "open"

    def test_the_agent_cannot_undo_the_firewall(self, probe: dict[str, Any]) -> None:
        assert probe["nft_flush_rc"] != 0
        assert probe["tcp_public_https_after_flush"] == "open"

    def test_the_probe_ran_unprivileged(self, probe: dict[str, Any]) -> None:
        assert probe["uid"] != 0
        assert probe["cap_eff"] == "0000000000000000"


_DNS_LISTENER_SOURCE = dedent(
    """
    import socket, threading

    def serve_tcp(port):
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("0.0.0.0", port))
        listener.listen(8)
        while True:
            conn, _ = listener.accept()
            conn.sendall(b"ok")
            conn.close()

    def serve_udp(port):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("0.0.0.0", port))
        while True:
            _data, addr = sock.recvfrom(128)
            sock.sendto(b"pong", addr)

    threading.Thread(target=serve_tcp, args=(53,), daemon=True).start()
    threading.Thread(target=serve_udp, args=(53,), daemon=True).start()
    serve_tcp(8000)
    """
)

_RESTRICT_DNS_PROBE = _HELPERS + dedent(
    """
    import subprocess

    dns_target = os.environ["PROBE_TARGET"]

    def udp(host, port):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(5)
        try:
            s.connect((host, port))
            s.send(b"probe")
            s.recv(16)
            return "open"
        except OSError as exc:
            return type(exc).__name__
        finally:
            s.close()

    flush = subprocess.run(["nft", "flush", "ruleset"], capture_output=True, text=True)

    print(json.dumps({
        "tcp_dns": tcp(dns_target, 53),
        "udp_dns": udp(dns_target, 53),
        "tcp_other_port": tcp(dns_target, 8000),
        "nft_flush_rc": flush.returncode,
    }))
    """
)


@pytest.mark.integration
class TestRestrictedResolverEgress:
    """Outside a compose stack the container's resolv.conf points at the
    host's resolvers, which sit in a private range. Restrict mode keeps
    exactly their port 53 reachable so names still resolve; every other port
    on those addresses stays refused."""

    @pytest.fixture(scope="class")
    def probe(
        self,
        docker_client: Any,
        firewall_image: str,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> Iterator[dict[str, Any]]:
        with _lab(docker_client) as lab:
            dns_network = docker_client.networks.create(
                f"openscientist-firewall-dns-{lab.suffix}",
                driver="bridge",
                ipam={"config": [{"subnet": "10.245.0.0/16", "gateway": "10.245.0.1"}]},
            )
            lab.extra_networks.append(dns_network)
            name = f"openscientist-firewall-dns-{lab.suffix}"
            listener = docker_client.containers.create(
                TEST_IMAGE,
                entrypoint=["python3", "-c", _DNS_LISTENER_SOURCE],
                name=name,
            )
            lab.started.append(listener)
            dns_network.connect(listener)
            listener.start()
            _wait_listening(listener, 53)
            _wait_listening(listener, 8000)
            dns_ip = lab.address_of(name, dns_network)
            # A default-bridge container inherits the host's resolv.conf, whose
            # resolvers sit in a private range; the mount reproduces that shape.
            resolv_conf = tmp_path_factory.mktemp("resolv") / "resolv.conf"
            resolv_conf.write_text(f"nameserver {dns_ip}\n", encoding="utf-8")
            yield lab.run_probe(
                "",
                _RESTRICT_DNS_PROBE,
                dns_ip,
                mode="restrict",
                extra_networks=[dns_network],
                resolv_conf=resolv_conf,
            )

    def test_dns_to_the_resolver_is_allowed_on_port_53(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_dns"] == "open"
        assert probe["udp_dns"] == "open"

    def test_every_other_port_on_the_resolver_is_blocked(self, probe: dict[str, Any]) -> None:
        assert probe["tcp_other_port"] == "TimeoutError"


#: The launcher's exec endpoint runs the code through the executor's import
#: sandbox, which allows requests but not socket or subprocess, so the probe
#: reaches the same endpoints over HTTP and reads the capability drop from
#: /proc instead of running nft.
_LAUNCHER_RESTRICT_PROBE = dedent(
    """
    import json, os, requests

    def http_probe(url):
        try:
            requests.get(url, timeout=5)
            return "open"
        except Exception as exc:
            return type(exc).__name__

    caps = ""
    for line in open("/proc/self/status"):
        if line.startswith("CapEff:"):
            caps = line.split()[1]

    target = __PROBE_TARGET__
    print(json.dumps({
        "uid": os.getuid(),
        "cap_eff": caps,
        "http_proxy": http_probe(f"http://{target}:8000/"),
        "http_broker": http_probe(f"http://{target}:8001/"),
        "http_postgres": http_probe(f"http://{target}:8002/"),
        "http_unallowlisted": http_probe(f"http://{target}:9000/"),
        "http_metadata": http_probe("http://169.254.169.254/"),
        "http_public": http_probe("https://www.google.com/generate_204"),
    }))
    """
)


@pytest.mark.integration
class TestLauncherRestrictedExecutor:
    """The launcher creates the executor container and the same restrict
    firewall the web's direct path installed holds through it: the web's
    endpoints stay open, the private ranges do not, and the privilege drop
    sticks. The probe runs as the executor's own code."""

    @pytest.fixture(scope="class")
    def probe(
        self,
        docker_client: Any,
        firewall_image: str,
        tmp_path_factory: pytest.TempPathFactory,
    ) -> Iterator[dict[str, Any]]:
        if problem := executor_image_problem():
            pytest.skip(problem)
        with _lab(docker_client) as lab:
            # The web's services on the shared network: proxy and broker are
            # allowlisted for the executor, Postgres is not.
            listener = lab.start_listener("web", [8000, 8001, 8002, 9000])
            jobs_root = tmp_path_factory.mktemp("launcher-jobs")
            job_id = str(uuid4())
            (jobs_root / job_id / "out").mkdir(parents=True)
            digest = str(docker_client.images.get(EXECUTOR_IMAGE).attrs["Id"])
            launcher = Launcher.start(
                tmp_path_factory.mktemp("launcher"),
                jobs_root,
                LAUNCHER_NETWORK=lab.network.name,
                AGENT_IMAGE_DIGESTS=digest,
                EXECUTOR_IMAGE_DIGESTS=digest,
            )
            try:
                code = _LAUNCHER_RESTRICT_PROBE.replace("__PROBE_TARGET__", repr(listener))
                body = {
                    "job_id": job_id,
                    "timeout_seconds": 120,
                    "memory_mib": 512,
                    "cpu": 0.25,
                    "egress_allow": [f"{listener}:8000", f"{listener}:8001"],
                    "input": {
                        "code": code,
                        "language": "python",
                        "data_path": None,
                        "output_dir": str(jobs_root / job_id / "out"),
                        "timeout": 90,
                        "description": "",
                        "iteration": 0,
                        "data_files": [],
                    },
                }
                with launcher.client() as client:
                    response = client.post("/v1/exec", json=body)
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload["exit_code"] == 0, payload["stdout"]
                result = json.loads(payload["stdout"])
                assert result["success"] is True, result
                line = next(
                    line for line in result["output"].splitlines() if '"http_proxy"' in line
                )
                yield json.loads(line)
            finally:
                launcher.close()

    def test_the_proxy_and_broker_stay_reachable(self, probe: dict[str, Any]) -> None:
        assert probe["http_proxy"] == "open"
        assert probe["http_broker"] == "open"

    def test_postgres_is_not_allowlisted_for_the_executor(self, probe: dict[str, Any]) -> None:
        assert probe["http_postgres"] == "ConnectTimeout"

    def test_an_unallowlisted_port_on_a_private_host_is_blocked(
        self, probe: dict[str, Any]
    ) -> None:
        assert probe["http_unallowlisted"] == "ConnectTimeout"

    def test_the_metadata_endpoint_is_unreachable(self, probe: dict[str, Any]) -> None:
        assert probe["http_metadata"] == "ConnectTimeout"

    def test_public_internet_stays_reachable(self, probe: dict[str, Any]) -> None:
        assert probe["http_public"] == "open"

    def test_the_probe_ran_as_the_executor_user(self, probe: dict[str, Any]) -> None:
        assert probe["uid"] != 0
        assert probe["cap_eff"] == "0000000000000000"
