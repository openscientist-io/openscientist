"""Tests for :mod:`openscientist.airgap.probes`.

Every probe is exercised on both branches: the "air-gap holds" path (probe
behaved as the policy expected — DNS resolution refused, TCP connect
refused, etc.) and the "air-gap broken" path (probe succeeded when it
shouldn't have, indicating a leaky deployment). The aggregator's
``airgap_holds`` is the single contract downstream callers read.

Subprocess and network calls are monkeypatched so the suite is hermetic
and fast (<200 ms total) — no real DNS queries, no real TCP connects.
"""

from __future__ import annotations

import socket
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from openscientist.airgap.probes import (
    ProbeResult,
    ProbeSetSummary,
    probe_curl_external_should_fail,
    probe_dns_external_should_fail,
    probe_dns_unique_subdomain_should_fail,
    probe_git_ls_remote_should_fail,
    probe_pip_install_should_fail,
    probe_python_urllib_should_fail,
    probe_tcp_external_should_fail,
    probe_tcp_internal_should_pass,
    run_airgap_probe_set,
    run_negative_probe_set,
    run_positive_probe_set,
)

# --------------------------------------------------------- ProbeResult


class TestProbeResult:
    def test_passed_when_actual_matches_expected_fail(self) -> None:
        r = ProbeResult(name="t", description="d", expected="fail", actual="fail", duration_ms=1)
        assert r.passed is True

    def test_passed_when_actual_matches_expected_pass(self) -> None:
        r = ProbeResult(name="t", description="d", expected="pass", actual="pass", duration_ms=1)
        assert r.passed is True

    def test_failed_when_negative_probe_unexpectedly_succeeds(self) -> None:
        # Negative probe expected to fail but the host was reachable —
        # air-gap is broken.
        r = ProbeResult(name="t", description="d", expected="fail", actual="pass", duration_ms=1)
        assert r.passed is False

    def test_failed_when_positive_probe_unreachable(self) -> None:
        # The internal LLM didn't answer — the job will fail anyway.
        r = ProbeResult(name="t", description="d", expected="pass", actual="fail", duration_ms=1)
        assert r.passed is False

    def test_skipped_counts_as_neither_pass_nor_fail(self) -> None:
        r = ProbeResult(name="t", description="d", expected="fail", actual="skipped", duration_ms=1)
        # "skipped" matches neither expectation; passed is False but the
        # summary's failed count excludes skipped.
        assert r.passed is False

    def test_as_dict_has_attestation_fields(self) -> None:
        r = ProbeResult(
            name="t",
            description="d",
            expected="fail",
            actual="fail",
            duration_ms=12,
            details="info",
        )
        d = r.as_dict()
        assert d["passed"] is True
        assert d["duration_ms"] == 12
        assert {"name", "expected", "actual", "details"}.issubset(d.keys())


# --------------------------------------------------------- DNS probes


class TestDnsExternal:
    def test_resolves_unexpectedly_means_airgap_broken(self) -> None:
        with patch("socket.gethostbyname", return_value="93.184.216.34") as gh:
            result = probe_dns_external_should_fail("example.com")
        gh.assert_called_once()
        assert result.expected == "fail"
        assert result.actual == "pass"
        assert result.passed is False
        assert "external resolver" in result.details

    def test_resolve_failure_confirms_airgap(self) -> None:
        with patch("socket.gethostbyname", side_effect=socket.gaierror("no resolve")):
            result = probe_dns_external_should_fail("example.com")
        assert result.actual == "fail"
        assert result.passed is True

    def test_oserror_also_confirms_airgap(self) -> None:
        # Some resolvers raise OSError rather than gaierror on no-route.
        with patch("socket.gethostbyname", side_effect=OSError("network unreachable")):
            result = probe_dns_external_should_fail("example.com")
        assert result.actual == "fail"
        assert result.passed is True

    def test_socket_timeout_restored_after_call(self) -> None:
        # Sentinel: the probe sets the default timeout briefly; if it leaks
        # to other tests they'd flake. Verify it's None after the call.
        with patch("socket.gethostbyname", side_effect=socket.gaierror("no")):
            probe_dns_external_should_fail("example.com")
        assert socket.getdefaulttimeout() is None


class TestDnsUniqueSubdomain:
    def test_unique_token_in_details(self) -> None:
        with patch("socket.gethostbyname", side_effect=socket.gaierror("no")):
            result = probe_dns_unique_subdomain_should_fail("example.com")
        assert result.passed is True
        # The token should be in the details (16 hex chars per secrets.token_hex(8)).
        assert "token=" in result.details
        assert "parent=example.com" in result.details

    def test_two_calls_use_different_tokens(self) -> None:
        # The point is that a forwarding resolver couldn't have cached this
        # exact subdomain — each call gets a fresh token.
        with patch("socket.gethostbyname", side_effect=socket.gaierror("no")):
            r1 = probe_dns_unique_subdomain_should_fail("example.com")
            r2 = probe_dns_unique_subdomain_should_fail("example.com")
        token1 = r1.details.split("token=")[1].split()[0]
        token2 = r2.details.split("token=")[1].split()[0]
        assert token1 != token2


# --------------------------------------------------------- TCP probes


class TestTcpExternal:
    def test_connection_unexpectedly_succeeds_means_airgap_broken(self) -> None:
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda self: self  # type: ignore[misc]
        mock_sock.__exit__ = lambda self, *args: None  # type: ignore[misc]
        with patch("socket.create_connection", return_value=mock_sock):
            result = probe_tcp_external_should_fail("1.1.1.1", 443)
        assert result.actual == "pass"
        assert result.passed is False

    def test_connection_refused_confirms_airgap(self) -> None:
        with patch("socket.create_connection", side_effect=OSError("refused")):
            result = probe_tcp_external_should_fail("1.1.1.1", 443)
        assert result.actual == "fail"
        assert result.passed is True
        assert "refused" in result.error


class TestTcpInternal:
    """The positive-probe mirror. Internal endpoints MUST succeed."""

    def test_reachable_internal_passes(self) -> None:
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda self: self  # type: ignore[misc]
        mock_sock.__exit__ = lambda self, *args: None  # type: ignore[misc]
        with patch("socket.create_connection", return_value=mock_sock):
            result = probe_tcp_internal_should_pass("10.0.0.5", 8443)
        assert result.actual == "pass"
        assert result.passed is True

    def test_unreachable_internal_fails(self) -> None:
        # If this fires for real, the LLM mirror is down — job will fail.
        with patch("socket.create_connection", side_effect=OSError("no route")):
            result = probe_tcp_internal_should_pass("10.0.0.5", 8443)
        assert result.actual == "fail"
        assert result.passed is False
        assert "no route" in result.error


# --------------------------------------------------------- HTTP via urllib


class TestPythonUrllib:
    def test_url_unexpectedly_reachable_means_airgap_broken(self) -> None:
        # An agent-authored urllib.request.urlopen() succeeded against the
        # public internet — the kernel-namespace boundary in §10.2 isn't
        # holding.
        mock_resp = MagicMock()
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = probe_python_urllib_should_fail("https://example.com")
        assert result.actual == "pass"
        assert result.passed is False
        mock_resp.close.assert_called_once()

    def test_url_unreachable_confirms_airgap(self) -> None:
        from urllib.error import URLError

        with patch("urllib.request.urlopen", side_effect=URLError("no route")):
            result = probe_python_urllib_should_fail("https://example.com")
        assert result.passed is True

    def test_oserror_unreachable_also_confirms(self) -> None:
        with patch("urllib.request.urlopen", side_effect=OSError("no route")):
            result = probe_python_urllib_should_fail("https://example.com")
        assert result.passed is True


# --------------------------------------------------------- shell-tool probes


def _fake_subprocess_run(returncode: int = 0, stdout: str = "", stderr: str = ""):
    """Build a fake ``subprocess.run`` that returns a CompletedProcess-like obj."""

    def _runner(cmd, **kwargs):
        completed = MagicMock(spec=subprocess.CompletedProcess)
        completed.returncode = returncode
        completed.stdout = stdout
        completed.stderr = stderr
        return completed

    return _runner


class TestCurlExternal:
    def test_curl_failed_confirms_airgap(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=_fake_subprocess_run(returncode=7, stderr="curl: (7) Failed to connect"),
        ):
            result = probe_curl_external_should_fail("https://example.com")
        assert result.actual == "fail"
        assert result.passed is True
        assert "Failed to connect" in result.details

    def test_curl_succeeded_means_airgap_broken(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=_fake_subprocess_run(returncode=0, stdout="<html>...</html>"),
        ):
            result = probe_curl_external_should_fail("https://example.com")
        assert result.actual == "pass"
        assert result.passed is False
        assert "reachable" in result.error.lower()

    def test_curl_missing_is_skipped_not_failed(self) -> None:
        # On a minimal image without curl the probe shouldn't conclude
        # "air-gap holds" just because the binary's absent.
        with patch("subprocess.run", side_effect=FileNotFoundError("curl")):
            result = probe_curl_external_should_fail("https://example.com")
        assert result.actual == "skipped"
        # Skipped probes don't contribute to passed/failed counts.
        assert result.passed is False


class TestPipInstall:
    def test_pip_failure_confirms_airgap(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=_fake_subprocess_run(returncode=1, stderr="Could not find a version"),
        ):
            result = probe_pip_install_should_fail("nonexistent-pkg")
        assert result.passed is True

    def test_pip_succeeding_for_nonexistent_pkg_means_private_mirror(self) -> None:
        # A pip exit 0 for a never-existed package implies somebody redirected
        # the request — usually a private mirror that returns nonsense.
        # Either way the air-gap policy is suspicious.
        with patch(
            "subprocess.run",
            side_effect=_fake_subprocess_run(returncode=0, stdout="ok"),
        ):
            result = probe_pip_install_should_fail("nonexistent-pkg")
        assert result.passed is False
        assert "unexpected resolution" in result.error


class TestGitLsRemote:
    def test_failure_confirms_airgap(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=_fake_subprocess_run(returncode=128, stderr="Could not resolve host"),
        ):
            result = probe_git_ls_remote_should_fail()
        assert result.passed is True

    def test_success_means_public_repo_reachable(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=_fake_subprocess_run(returncode=0, stdout="abc123\tHEAD\n"),
        ):
            result = probe_git_ls_remote_should_fail()
        assert result.passed is False

    def test_timeout_normalized_to_fail(self) -> None:
        with patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd=["git"], timeout=5),
        ):
            result = probe_git_ls_remote_should_fail()
        # Timeout in an airgap deployment = blackhole route = expected
        assert result.passed is True


# --------------------------------------------------------- aggregator + summary


class TestProbeSetSummary:
    def test_counts(self) -> None:
        summary = ProbeSetSummary(
            results=[
                ProbeResult("a", "", "fail", "fail", 1),  # passed
                ProbeResult("b", "", "fail", "pass", 1),  # failed (broken)
                ProbeResult("c", "", "fail", "skipped", 1),  # skipped
                ProbeResult("d", "", "pass", "pass", 1),  # passed
            ]
        )
        assert summary.total == 4
        assert summary.passed == 2
        assert summary.failed == 1  # 'b' only — 'c' skipped doesn't count
        assert summary.skipped == 1
        assert summary.airgap_holds is False  # the one fail breaks the claim

    def test_airgap_holds_when_all_skipped_or_passed(self) -> None:
        # All non-skipped probes behaved as expected — air-gap holds even
        # if some couldn't run (binary missing).
        summary = ProbeSetSummary(
            results=[
                ProbeResult("a", "", "fail", "fail", 1),
                ProbeResult("b", "", "fail", "skipped", 1),
                ProbeResult("c", "", "pass", "pass", 1),
            ]
        )
        assert summary.airgap_holds is True

    def test_as_dict_round_trips(self) -> None:
        summary = ProbeSetSummary(results=[ProbeResult("a", "d", "fail", "fail", 1, details="ok")])
        d = summary.as_dict()
        assert d["total"] == 1
        assert d["airgap_holds"] is True
        assert len(d["results"]) == 1
        assert d["results"][0]["name"] == "a"


class TestNegativeProbeSet:
    @pytest.fixture
    def airgap_is_holding(self) -> None:
        """Patch every reachable probe to behave as the air-gap policy expects."""
        with (
            patch("socket.gethostbyname", side_effect=socket.gaierror("no")),
            patch("socket.create_connection", side_effect=OSError("refused")),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            patch(
                "subprocess.run",
                side_effect=_fake_subprocess_run(returncode=1, stderr="connect refused"),
            ),
        ):
            yield

    def test_all_probes_pass_when_airgap_holds(self, airgap_is_holding: None) -> None:
        summary = run_negative_probe_set(timeout=1)
        # Every negative probe should report "actual=fail" → passed=True.
        assert summary.airgap_holds is True
        assert summary.failed == 0

    def test_summary_includes_expected_probe_categories(self, airgap_is_holding: None) -> None:
        summary = run_negative_probe_set(timeout=1)
        names = {r.name.split("::")[0] for r in summary.results}
        # The categories the RFC §14 list calls out.
        assert "dns_external" in names
        assert "dns_unique_subdomain" in names
        assert "tcp_external" in names
        assert "python_urllib" in names
        assert "curl_external" in names
        assert "pip_install_should_fail" in names
        assert "git_ls_remote" in names

    def test_one_leak_breaks_the_claim(self) -> None:
        # DNS works but everything else is blocked — still a leak.
        with (
            patch("socket.gethostbyname", return_value="93.184.216.34"),
            patch("socket.create_connection", side_effect=OSError("refused")),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            patch(
                "subprocess.run",
                side_effect=_fake_subprocess_run(returncode=1, stderr="refused"),
            ),
        ):
            summary = run_negative_probe_set(timeout=1)
        # DNS resolves contribute multiple failures (one per hostname).
        assert summary.failed > 0
        assert summary.airgap_holds is False


class TestPositiveProbeSet:
    def test_reachable_endpoints_pass(self) -> None:
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda self: self  # type: ignore[misc]
        mock_sock.__exit__ = lambda self, *args: None  # type: ignore[misc]
        with patch("socket.create_connection", return_value=mock_sock):
            summary = run_positive_probe_set([("10.0.0.5", 8443), ("10.0.0.6", 9000)], timeout=1)
        assert summary.total == 2
        assert summary.airgap_holds is True

    def test_unreachable_endpoint_fails_the_set(self) -> None:
        with patch("socket.create_connection", side_effect=OSError("no route")):
            summary = run_positive_probe_set([("10.0.0.5", 8443)], timeout=1)
        assert summary.failed == 1
        assert summary.airgap_holds is False


class TestRunAirgapProbeSet:
    def test_combines_negative_and_positive(self) -> None:
        mock_sock = MagicMock()
        mock_sock.__enter__ = lambda self: self  # type: ignore[misc]
        mock_sock.__exit__ = lambda self, *args: None  # type: ignore[misc]
        with (
            patch("socket.gethostbyname", side_effect=socket.gaierror("no")),
            patch(
                "socket.create_connection",
                side_effect=lambda addr, **kw: (
                    mock_sock
                    if addr[0].startswith("10.0.")
                    else (_ for _ in ()).throw(OSError("refused"))
                ),
            ),
            patch("urllib.request.urlopen", side_effect=OSError("refused")),
            patch(
                "subprocess.run",
                side_effect=_fake_subprocess_run(returncode=1, stderr="refused"),
            ),
        ):
            summary = run_airgap_probe_set([("10.0.0.5", 8443)], timeout=1)
        # The set is the union of negative + positive. airgap_holds when
        # every negative probe failed AND every positive succeeded.
        assert summary.airgap_holds is True
        # And the positive probe is in there.
        positive_names = [r.name for r in summary.results if r.name.startswith("tcp_internal")]
        assert positive_names
