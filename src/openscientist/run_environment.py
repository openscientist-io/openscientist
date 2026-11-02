"""The one-line description of the environment a run recorded in ``Job.version_info``."""

from __future__ import annotations

from openscientist.agent.base import AgentBackend
from openscientist.agent.factory import agent_class_for_backend


def _harness_display_name(harness: str) -> str:
    """The harness's own display name, or the recorded id for an unknown one."""
    try:
        return agent_class_for_backend(AgentBackend(harness)).display_name
    except ValueError:
        return harness


def run_environment_text(version_info: dict[str, str] | None) -> str | None:
    """One line naming the build, harness and container a run recorded, or
    None when it recorded nothing."""
    if not version_info:
        return None
    parts: list[str] = []
    commit = version_info.get("openscientist_commit")
    build_time = version_info.get("openscientist_build_time")
    if commit or build_time:
        build = "OpenScientist"
        if commit:
            build += f" {commit}"
        if build_time:
            build += f" built {build_time}"
        parts.append(build)
    harness = version_info.get("agent_harness")
    if harness:
        version = version_info.get("agent_harness_version", "")
        parts.append(f"{_harness_display_name(harness)} {version}".strip())
    sdk_version = version_info.get("claude_code_sdk_version")
    if sdk_version:
        parts.append(f"Claude Agent SDK {sdk_version}")
    container = version_info.get("docker_container_id")
    if container:
        parts.append(f"container {container}")
    if not parts:
        return None
    return "Run environment: " + " · ".join(parts)
