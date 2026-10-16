"""Enforcement tests: a new agent backend cannot silently skip a behavior.

The ``AbstractAgent`` contract already makes each backend-divergent behavior
an abstract member (so abc + mypy reject an incomplete subclass) and
``__init_subclass__`` enforces the ``backend`` ClassVar. These runtime guards
add the cross-cutting checks the type system cannot express: every concrete
agent is reachable from the factory, every ``AgentBackend`` has exactly one
agent, every provider resolves to a concrete agent, and each backend's
prompts are fully substituted (no leftover sentinels, no foreign vocabulary).
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping

import pytest
from claude_agent_sdk.types import AgentDefinition

from openscientist.agent.base import AbstractAgent, AgentBackend, IterationResult, TurnOutcome

# Importing the concrete agents registers them as AbstractAgent subclasses.
from openscientist.agent.claude_code_agent import ClaudeCodeAgent  # noqa: F401
from openscientist.agent.codex_agent import CodexAgent
from openscientist.agent.factory import (
    agent_class_for_provider_id,
    backend_for_provider_id,
)
from openscientist.agent.omp_agent import OmpAgent  # noqa: F401
from openscientist.prompts.common import BackendFragments
from openscientist.providers import provider_class, provider_ids
from openscientist.providers.base import OpenAiWireCompatible, Provider


def _concrete_agent_classes() -> set[type[AbstractAgent[Provider]]]:
    """All production (non-test) concrete AbstractAgent subclasses."""
    found: set[type[AbstractAgent[Provider]]] = set()

    def walk(cls: type[AbstractAgent[Provider]]) -> None:
        for sub in cls.__subclasses__():
            walk(sub)
            if not inspect.isabstract(sub) and sub.__module__.startswith("openscientist."):
                found.add(sub)

    walk(AbstractAgent)  # type: ignore[type-abstract]
    return found


def _concrete_provider_classes() -> set[type[Provider]]:
    """All production (non-test) concrete Provider subclasses.

    Imports every registered provider module first so the subclass tree is
    complete regardless of import order.
    """
    for provider_id in provider_ids():
        provider_class(provider_id)  # forces the on-demand import

    found: set[type[Provider]] = set()

    def walk(cls: type[Provider]) -> None:
        for sub in cls.__subclasses__():
            walk(sub)
            if not inspect.isabstract(sub) and sub.__module__.startswith(
                "openscientist.providers."
            ):
                found.add(sub)

    walk(Provider)  # type: ignore[type-abstract]
    return found


def test_every_concrete_agent_declares_a_backend() -> None:
    for cls in _concrete_agent_classes():
        assert isinstance(cls.backend, AgentBackend), cls


def test_every_concrete_agent_declares_a_file_write_tool() -> None:
    # The report prompt names this tool verbatim so the model invokes it rather
    # than printing the call as text. A new backend that omits it would silently
    # ship a vague prompt, so it is enforced in __init_subclass__ and here.
    for cls in _concrete_agent_classes():
        assert isinstance(cls.file_write_tool, str) and cls.file_write_tool, cls


def test_every_backend_has_exactly_one_agent() -> None:
    by_backend: dict[AgentBackend, list[type[AbstractAgent[Provider]]]] = {}
    for cls in _concrete_agent_classes():
        by_backend.setdefault(cls.backend, []).append(cls)
    assert set(by_backend) == set(AgentBackend), "every AgentBackend member needs an agent"
    for backend, classes in by_backend.items():
        assert len(classes) == 1, f"{backend} has multiple agents: {classes}"


def test_every_provider_resolves_to_a_concrete_agent() -> None:
    concrete = _concrete_agent_classes()
    for provider_id in provider_ids():
        agent_cls = agent_class_for_provider_id(provider_id)
        assert agent_cls in concrete, provider_id
        # The id-keyed backend resolver agrees with the resolved agent class.
        assert backend_for_provider_id(provider_id) is agent_cls.backend, provider_id


def test_every_provider_class_is_registered() -> None:
    # A Provider defined but missing from the single registry is "half-wired":
    # it fails loudly at use-time, but nothing flags the omission. Catch it here
    # so adding a backend means adding its registry entry too.
    registered = {provider_class(pid) for pid in provider_ids()}
    for cls in _concrete_provider_classes():
        assert cls in registered, f"{cls.__name__} is not in providers._PROVIDER_CLASS_PATHS"


def test_every_provider_declares_its_own_harness_routing() -> None:
    """Every provider must answer how a provider-agnostic harness reaches it.

    This is the gap that shipped. ``harness_env`` defaulted to ``{}``, so a
    provider nobody wired sent omp to the vendor with the real credential,
    bypassing the key-replacement proxy. Under air-gap that hangs until the turn
    times out; without air-gap it succeeds silently, which is worse. The existing
    structural checks did not catch it because they cover provider-to-agent
    wiring, not reachability.

    It is now abstract, so this guards against someone reintroducing a permissive
    default on a base class and letting providers inherit silence again. The
    routing values themselves are asserted per provider in ``test_llm_proxy``,
    which already owns the per-provider credential recipes.
    """
    for provider_id in provider_ids():
        cls = provider_class(provider_id)
        owner = next(k for k in cls.__mro__ if "harness_env" in k.__dict__)
        assert owner is not Provider, (
            f"{provider_id} inherits harness_env from the abstract base, so it "
            "declares no routing for a provider-agnostic harness"
        )


#: Providers for which inheriting ``OpenAiWireCompatible.harness_env`` is correct:
#: the endpoint really is OpenAI's, so an unproxied harness should use its default.
_HOSTED_OPENAI_FAMILY = {"openai", "azure-openai"}


def test_self_hosted_providers_do_not_inherit_the_openai_default() -> None:
    """The inherited default is silently wrong for a self-hosted provider.

    ``OpenAiWireCompatible.harness_env`` returns nothing when no proxy is active,
    which leaves omp on its built-in default of ``api.openai.com``. For OpenAI and
    Azure that is right. For anything self-hosted it means a job configured against
    a local server quietly talks to OpenAI instead, the same silent-reachability
    failure that motivated making ``harness_env`` abstract. Inheriting is therefore
    opt-in, so a new OpenAI-wire provider has to choose.

    Anchored on the class that actually defines the default. It previously named
    ``CodexCompatible``, and when the default moved up to the wire layer the
    condition stopped matching anything and the guard passed vacuously.
    """
    checked = 0
    for provider_id in provider_ids():
        cls = provider_class(provider_id)
        if not issubclass(cls, OpenAiWireCompatible):
            continue
        owner = next(k for k in cls.__mro__ if "harness_env" in k.__dict__)
        if owner is OpenAiWireCompatible:
            checked += 1
            assert provider_id in _HOSTED_OPENAI_FAMILY, (
                f"{provider_id} inherits the OpenAI harness default, so an unproxied "
                "run points at api.openai.com. Override harness_env to name its real "
                f"endpoint, or add it to {sorted(_HOSTED_OPENAI_FAMILY)} if it is "
                "genuinely OpenAI-hosted"
            )
    assert checked, "guard matched no provider, so it is no longer testing anything"


def test_every_concrete_agent_declares_a_display_name() -> None:
    # Enforced in __init_subclass__; a backend missing its label would otherwise
    # only surface at UI render time.
    for cls in _concrete_agent_classes():
        assert isinstance(cls.display_name, str) and cls.display_name, cls


def test_prompts_are_fully_substituted() -> None:
    for cls in _concrete_agent_classes():
        assert isinstance(cls.prompt_fragments(), BackendFragments)
        for text in (
            cls.system_prompt(),
            cls.job_doc(use_hypotheses=True, phenix_available=True),
            cls.chat_doc(),
            cls.discovery_system_prompt(use_hypotheses=True, phenix_available=True),
        ):
            assert text, cls
            assert "{{" not in text and "}}" not in text, f"unsubstituted sentinel in {cls}"


def test_codex_prompts_drop_claude_vocabulary() -> None:
    for text in (
        CodexAgent.chat_doc(),
        CodexAgent.discovery_system_prompt(use_hypotheses=True, phenix_available=True),
    ):
        assert "Claude's" not in text
        assert "`.claude/skills/`" not in text


def test_concrete_subclass_without_backend_is_rejected() -> None:
    """__init_subclass__ rejects a concrete agent that omits the backend
    ClassVar (abc cannot enforce a plain ClassVar)."""
    with pytest.raises(TypeError, match="backend"):

        class _NoBackend(AbstractAgent[Provider]):
            # All abstract members implemented, so the class is concrete, but
            # `backend` is intentionally not set.
            @classmethod
            def prompt_fragments(cls) -> BackendFragments:
                raise NotImplementedError

            @classmethod
            def discovery_system_prompt(
                cls,
                *,
                use_hypotheses: bool = False,
                phenix_available: bool = False,
                experts: Mapping[str, AgentDefinition] | None = None,
            ) -> str:
                return ""

            async def prepare_job_workspace(self, *, use_hypotheses: bool = False) -> None:
                return None

            async def run_iteration(
                self, prompt: str, *, reset_session: bool = False
            ) -> IterationResult:
                return IterationResult(
                    outcome=TurnOutcome.COMPLETED, output="", tool_calls=0, transcript=[]
                )

            async def shutdown(self) -> None:
                return None
