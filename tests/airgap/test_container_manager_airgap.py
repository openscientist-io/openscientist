"""Sentinel for executor container network isolation in air-gap mode.

Codex Review-6 BUG (fixed): the executor inherited the agent network in
air-gap mode, so the MCP_TOOLS_LOCAL_ONLY classification of ``execute_code``
was incorrect — `code_executor.py` opens SPARQL endpoints and imports
``requests``. RFC §10.2 mandates the executor run with a fully isolated
network namespace (`network="none"`); this test pins that.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _settings(*, airgap_enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        container=SimpleNamespace(
            executor_image="exec:latest",
            executor_memory="2g",
            executor_cpu=0.5,
            executor_timeout=120,
            agent_network=None,
            host_project_dir=None,
            container_app_dir="/app",
        ),
        airgap=SimpleNamespace(enabled=airgap_enabled),
    )


class _RunWasCalled(BaseException):
    """Sentinel raised from the mocked containers.run to short-circuit
    execute_code right after we capture the call kwargs. Subclasses
    BaseException (not Exception) so the broad ``except Exception:`` in
    execute_code lets it propagate."""

    def __init__(self, kwargs: dict) -> None:
        self.kwargs = kwargs


def _run_execute_code(cm, *, expect_network: str) -> None:
    """Drive ``ContainerManager.execute_code`` until ``client.containers.run``
    is called; capture the ``network`` kwarg; raise out to skip downstream
    cleanup paths we don't care about."""
    cm._client = MagicMock()

    def _capture_run(*args, **kwargs):
        raise _RunWasCalled(kwargs)

    cm._client.containers.run.side_effect = _capture_run

    with patch(
        "openscientist.job_container.resolve_docker_network",
        return_value="bridge",
    ):
        try:
            cm.execute_code(
                code="print(1)",
                language="python",
                job_id="job-42",
                output_dir=__import__("pathlib").Path("/tmp"),
                iteration=1,
                description="test",
            )
        except _RunWasCalled as exc:
            assert exc.kwargs["network"] == expect_network, (
                f"expected network={expect_network!r}, got {exc.kwargs.get('network')!r}"
            )
            return
        # If we got here, containers.run was never called.
        raise AssertionError("expected containers.run to be called")


class TestExecutorNetworkInAirgap:
    def test_airgap_uses_network_none(self) -> None:
        from openscientist.container_manager import ContainerManager

        with patch(
            "openscientist.container_manager.get_settings",
            return_value=_settings(airgap_enabled=True),
        ):
            cm = ContainerManager()
        # The first get_settings was during __init__; patch for the execute_code
        # call too.
        with patch(
            "openscientist.container_manager.get_settings",
            return_value=_settings(airgap_enabled=True),
        ):
            _run_execute_code(cm, expect_network="none")

    def test_non_airgap_uses_resolved_docker_network(self) -> None:
        # Regression sentinel: the legacy non-airgap path resolves the
        # configured agent network.
        from openscientist.container_manager import ContainerManager

        with patch(
            "openscientist.container_manager.get_settings",
            return_value=_settings(airgap_enabled=False),
        ):
            cm = ContainerManager()
        with patch(
            "openscientist.container_manager.get_settings",
            return_value=_settings(airgap_enabled=False),
        ):
            _run_execute_code(cm, expect_network="bridge")
