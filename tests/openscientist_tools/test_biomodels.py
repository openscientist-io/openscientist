"""Tests for the standalone `search_biomodels` / `fetch_biomodel` tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import requests
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from openscientist.knowledge_state import KnowledgeState
from openscientist_tools import biomodels, state

_SBML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<sbml xmlns="http://www.sbml.org/sbml/level2/version4" level="2" version="4">\n'
    '<model id="m"><listOfSpecies><species id="A"/><species id="B"/></listOfSpecies>\n'
    '<listOfParameters><parameter id="k1" value="0.1"/></listOfParameters>\n'
    '<listOfReactions><reaction id="r1"/></listOfReactions></model></sbml>\n'
)


class _Resp:
    """Minimal stand-in for `requests.Response`."""

    def __init__(self, *, json_data: Any = None, content: bytes = b"", status: int = 200):
        self._json = json_data
        self.content = content
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._json


@pytest.fixture
def fake_biomodels(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Route `requests.get` inside the tool module to canned BioModels replies."""

    def _get(url: str, params: dict[str, Any] | None = None, **_: Any) -> _Resp:
        params = params or {}
        if url.endswith("/search"):
            return _Resp(
                json_data={
                    "matches": 2,
                    "models": [
                        {
                            "id": "BIOMD0000000462",
                            "name": "Proctor2012 - Amyloid-beta dimers",
                            "format": "SBML",
                            "submitter": "x",
                        },
                        {"id": "MODEL1234567890", "name": "Draft model", "format": "SBML"},
                        {"name": "no id -> skipped"},
                    ],
                }
            )
        if url.endswith("/model/download/BIOMD0000000462"):
            assert params.get("filename") == "BIOMD0000000462_url.xml"
            return _Resp(content=_SBML.encode())
        if url.endswith("/BIOMD0000000462"):
            return _Resp(
                json_data={
                    "name": "Proctor2012 - Amyloid-beta dimers",
                    "format": {"name": "SBML"},
                    "publication": {
                        "title": "Aggregation of amyloid-beta dimers",
                        "journal": "PLoS ONE",
                        "year": 2012,
                        "link": "http://identifiers.org/pubmed/22748062",
                    },
                    "files": {"main": [{"name": "BIOMD0000000462_url.xml"}]},
                }
            )
        if url.endswith("/BIOMD0000000999"):
            return _Resp(status=404)
        raise AssertionError(f"unexpected URL {url}")

    mock = MagicMock(side_effect=_get)
    monkeypatch.setattr(biomodels.requests, "get", mock)
    return mock


# ----- backend -----


def test_search_backend_parses_models(fake_biomodels: MagicMock) -> None:
    results = biomodels.search_biomodels_backend("amyloid", 10)
    assert [r["id"] for r in results] == ["BIOMD0000000462", "MODEL1234567890"]
    assert results[0]["url"].endswith("/BIOMD0000000462")
    _, kwargs = fake_biomodels.call_args
    assert kwargs["params"] == {"query": "amyloid", "numResults": 10, "format": "json"}
    # The live API over-returns on small numResults, so the cap is enforced client-side.
    assert len(biomodels.search_biomodels_backend("amyloid", 1)) == 1


def test_fetch_backend_writes_file_and_counts(fake_biomodels: MagicMock, tmp_path: Path) -> None:
    info = biomodels.fetch_biomodel_backend("BIOMD0000000462", tmp_path / "biomodels")
    assert info["path"] == tmp_path / "biomodels" / "BIOMD0000000462.xml"
    assert info["path"].read_text() == _SBML
    assert (info["n_species"], info["n_reactions"], info["n_parameters"]) == (2, 1, 1)
    assert info["sbml_level"] == "L2V4"
    assert info["publication_year"] == "2012"


# ----- tool bodies (in-process, KS persistence patched) -----


def test_search_tool_logs_and_formats(
    fake_biomodels: MagicMock, patched_ks_persistence: KnowledgeState
) -> None:
    out = biomodels.search_biomodels(query="amyloid", max_results=10, description="why")
    assert "BIOMD0000000462" in out and "Proctor2012" in out
    assert "fetch_biomodel" in out
    log = patched_ks_persistence.data["analysis_log"][-1]
    assert log["action"] == "search_biomodels"
    assert log["model_ids"] == ["BIOMD0000000462", "MODEL1234567890"]


def test_search_tool_reports_network_failure(
    monkeypatch: pytest.MonkeyPatch, patched_ks_persistence: KnowledgeState
) -> None:
    monkeypatch.setattr(
        biomodels.requests, "get", MagicMock(side_effect=requests.ConnectionError("down"))
    )
    out = biomodels.search_biomodels(query="x")
    assert out.startswith("❌ ERROR")
    assert patched_ks_persistence.data["analysis_log"][-1]["success"] is False


def test_fetch_tool_saves_under_provenance(
    fake_biomodels: MagicMock,
    patched_ks_persistence: KnowledgeState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state.STATE, "job_dir", tmp_path)
    out = biomodels.fetch_biomodel(model_id="biomd0000000462")
    saved = tmp_path / "provenance" / "biomodels" / "BIOMD0000000462.xml"
    assert saved.read_text() == _SBML
    assert "/output/biomodels/BIOMD0000000462.xml" in out
    assert "PLoS ONE" in out
    log = patched_ks_persistence.data["analysis_log"][-1]
    assert log["action"] == "fetch_biomodel"
    assert log["file"] == "provenance/biomodels/BIOMD0000000462.xml"


def test_fetch_tool_rejects_malformed_id(patched_ks_persistence: KnowledgeState) -> None:
    out = biomodels.fetch_biomodel(model_id="not-an-id")
    assert out.startswith("❌ ERROR")
    assert not patched_ks_persistence.data["analysis_log"]


def test_fetch_tool_reports_missing_model(
    fake_biomodels: MagicMock,
    patched_ks_persistence: KnowledgeState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(state.STATE, "job_dir", tmp_path)
    out = biomodels.fetch_biomodel(model_id="BIOMD0000000999")
    assert out.startswith("❌ ERROR") and "404" in out


# ----- registration on the stdio server -----


async def test_biomodels_tools_registered(
    tmp_path: Path,
    server_env: Callable[..., dict[str, str]],
    server_params: Callable[[dict[str, str]], StdioServerParameters],
) -> None:
    params = server_params(server_env(tmp_path))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
    names = {t.name for t in tools.tools}
    assert {"search_biomodels", "fetch_biomodel"} <= names
    fetch = next(t for t in tools.tools if t.name == "fetch_biomodel")
    assert "model_id" in fetch.inputSchema["properties"]


# ----- live -----


@pytest.mark.network
def test_live_biomodels_search_and_fetch(tmp_path: Path) -> None:
    results = biomodels.search_biomodels_backend("amyloid", 5)
    assert results, "BioModels search returned nothing for 'amyloid'"
    info = biomodels.fetch_biomodel_backend(results[0]["id"], tmp_path)
    assert info["path"].exists() and info["n_species"] > 0
