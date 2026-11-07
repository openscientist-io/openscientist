"""Turn prompts name omp's tools by their namespaced names, without touching
the scientist's own words.

The prompts are backend-agnostic and name MCP tools bare, which is right for
Claude and codex. omp exposes them as ``mcp__openscientist_tools_<name>``, so a
bare name comes back "Tool ... not found". The rewrite that fixes that must not
reach the research question, description or feedback, which the scientist wrote.
"""

from openscientist.knowledge_state import KnowledgeState
from openscientist.orchestrator.iteration import (
    build_consensus_prompt,
    build_initial_prompt,
    build_iteration_prompt,
    build_prompt_format_prompt,
    build_prompt_format_retry_prompt,
)
from openscientist.prompts.common import namespace_tool_mentions

_PREFIX = "mcp__openscientist_tools_"
_QUESTION = "Why does our `execute_code` helper drop rows, and does execute_code() retry?"


def _ks() -> KnowledgeState:
    return KnowledgeState("j1", _QUESTION, 10)


def test_the_initial_prompt_namespaces_our_instructions() -> None:
    prompt = build_initial_prompt(_QUESTION, 10, ["d.csv"], _ks(), tool_prefix=_PREFIX)
    assert f"`{_PREFIX}search_pubmed`" in prompt
    assert f"`{_PREFIX}save_iteration_summary`" in prompt


def test_the_initial_prompt_leaves_the_research_question_alone() -> None:
    """The question is the scientist's text. Rewriting a tool name inside it
    puts a question to the model that nobody asked."""
    prompt = build_initial_prompt(_QUESTION, 10, ["d.csv"], _ks(), tool_prefix=_PREFIX)
    assert _QUESTION in prompt


def test_the_iteration_prompt_leaves_feedback_alone() -> None:
    feedback = "Please stop calling `execute_code` on the raw file."
    prompt = build_iteration_prompt(
        2, 10, _ks(), pending_feedback=feedback, description=_QUESTION, tool_prefix=_PREFIX
    )
    assert feedback in prompt
    assert _QUESTION in prompt
    assert f"`{_PREFIX}update_knowledge_state`" in prompt


def test_the_consensus_prompt_namespaces_only_its_own_instruction() -> None:
    prompt = build_consensus_prompt(_QUESTION, tool_prefix=_PREFIX)
    assert f"`{_PREFIX}set_consensus_answer`" in prompt
    assert _QUESTION in prompt


def test_the_prompt_format_prompt_namespaces_only_its_own_instruction() -> None:
    prompt = build_prompt_format_prompt(
        _QUESTION, image_files=["figure.png"], other_files=["data.csv"], tool_prefix=_PREFIX
    )
    assert f"`{_PREFIX}set_formatted_prompt`" in prompt
    assert _QUESTION in prompt
    assert "data/figure.png" in prompt
    assert "data/data.csv" in prompt


def test_the_prompt_format_prompt_renders_the_vocabulary_from_the_sanitizer() -> None:
    """The allowed tags, attributes and layout classes are rendered from the
    lists the job page's sanitizer keeps, so the prompt and the sanitizer
    cannot drift apart."""
    from openscientist.formatted_prompt import ALLOWED_ATTRIBUTES, ALLOWED_TAGS, LAYOUT_CLASSES

    prompt = build_prompt_format_prompt(_QUESTION, image_files=[], other_files=[], tool_prefix="")
    for tag in sorted(ALLOWED_TAGS):
        assert tag in prompt
    for attributes in ALLOWED_ATTRIBUTES.values():
        for attribute in sorted(attributes):
            assert attribute in prompt
    for name in LAYOUT_CLASSES:
        assert name in prompt


def test_the_prompt_format_retry_prompt_restates_the_task() -> None:
    prompt = build_prompt_format_retry_prompt(
        _QUESTION, image_files=["figure.png"], other_files=[], tool_prefix=_PREFIX
    )
    assert "did NOT record" in prompt
    assert f"`{_PREFIX}set_formatted_prompt`" in prompt
    assert _QUESTION in prompt
    assert "data/figure.png" in prompt


def test_an_empty_prefix_changes_nothing() -> None:
    """Claude and codex call the tools bare."""
    prompt = build_initial_prompt(_QUESTION, 10, ["d.csv"], _ks(), tool_prefix="")
    assert "`execute_code`" in prompt
    assert _PREFIX not in prompt


def test_every_marked_form_is_namespaced() -> None:
    """Backticks, bold and a call, with or without a space before the bracket."""
    doc = 'Use **execute_code**, then search_pubmed("x"), then set_status ("done").'
    out = namespace_tool_mentions(doc, _PREFIX)
    assert f"**{_PREFIX}execute_code**" in out
    assert f'{_PREFIX}search_pubmed("x")' in out
    assert f'{_PREFIX}set_status ("done")' in out


def test_an_already_namespaced_name_is_left_alone() -> None:
    doc = f"Call `{_PREFIX}execute_code` and {_PREFIX}search_pubmed()."
    assert namespace_tool_mentions(doc, _PREFIX) == doc


def test_a_longer_identifier_is_not_a_tool() -> None:
    """``run_phenix_tool`` is a tool, ``_run_phenix_tool_impl`` is a helper a
    skill may show in an example."""
    doc = "See _run_phenix_tool_impl in the source."
    assert namespace_tool_mentions(doc, _PREFIX) == doc


def test_omp_builtin_tools_are_not_namespaced() -> None:
    """omp's own `read` and `write` are called bare."""
    doc = "Call the `write` tool, then `read`."
    assert namespace_tool_mentions(doc, _PREFIX) == doc
