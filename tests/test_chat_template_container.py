"""Integration tests for chat template availability inside real Docker images."""

from __future__ import annotations

import json
from contextlib import suppress
from pathlib import Path
from textwrap import dedent
from uuid import uuid4

import pytest

_CONTAINER_CHECK_SCRIPT = dedent(
    """
    import json
    from importlib import resources
    from pathlib import Path

    from openscientist.prompts.common import read_chat_template

    template = resources.files("openscientist.templates").joinpath("CHAT_CLAUDE.md")
    claude_dir = Path("/tmp/chat-template-test/.claude")
    claude_dir.mkdir(parents=True, exist_ok=True)

    rendered = claude_dir / "CLAUDE.md"
    rendered.write_text(read_chat_template(), encoding="utf-8")
    print(
        json.dumps(
            {
                "template_exists": template.is_file(),
                "rendered_exists": rendered.exists(),
                "first_line": rendered.read_text(encoding="utf-8").splitlines()[0]
                if rendered.exists()
                else None,
            }
        )
    )
    """
)


@pytest.mark.integration
class TestChatTemplateContainerIntegration:
    """Real-container checks for packaged chat template availability."""

    @pytest.fixture(scope="class")
    def docker_client(self):
        """Provide a Docker client or skip when Docker is unavailable."""
        import docker

        client = docker.from_env()
        try:
            client.ping()
        except Exception:
            client.close()
            pytest.skip("Docker not available")

        try:
            yield client
        finally:
            client.close()

    @pytest.fixture(scope="class")
    def built_web_image(self, docker_client):
        """Build the current web image and return its temporary tag."""
        import docker

        try:
            docker_client.images.get("openscientist-base:latest")
        except docker.errors.ImageNotFound:
            pytest.skip(
                "Base image 'openscientist-base:latest' not built. Build it before running integration tests."
            )

        repo_root = Path(__file__).resolve().parents[1]
        tag = f"openscientist-chat-template-test:{uuid4().hex[:12]}"

        try:
            docker_client.images.build(
                path=str(repo_root),
                dockerfile="Dockerfile",
                tag=tag,
                rm=True,
            )
        except docker.errors.BuildError as exc:
            # docker SDK 7.x returns ``build_log`` as an ``itertools._tee``
            # iterator, not a list — slicing it raises TypeError, which then
            # masks the actual build failure. Materialize first, then keep
            # the last 20 ``dict`` entries (skipping non-dict progress
            # frames the SDK occasionally yields).
            log_entries = [e for e in list(exc.build_log) if isinstance(e, dict)]
            log_tail = "\n".join(
                str(entry.get("stream", "")).rstrip() for entry in log_entries[-20:]
            )
            pytest.fail(f"Failed to build test web image {tag}:\n{log_tail}")

        try:
            yield tag
        finally:
            with suppress(Exception):
                docker_client.images.remove(tag, force=True)

    def test_web_image_can_render_chat_claude_md(self, docker_client, built_web_image):
        """The packaged chat template should be readable and writable inside the web image."""
        client = docker_client

        output = client.containers.run(
            image=built_web_image,
            entrypoint=["python", "-c"],
            command=[_CONTAINER_CHECK_SCRIPT],
            environment={
                "OPENSCIENTIST_SECRET_KEY": "integration-test-secret",
                "DATABASE_URL": "postgresql+asyncpg://test:test@localhost:5432/test",
            },
            remove=True,
            working_dir="/tmp",
        )

        payload = json.loads(output.decode("utf-8"))
        assert payload["template_exists"] is True
        assert payload["rendered_exists"] is True
        assert payload["first_line"] == "# OpenScientist Job Chat Assistant"
