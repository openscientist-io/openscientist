"""Unit tests for the Monarch Initiative REST client (MARDUK).

Network is mocked — these exercise request shaping, defensive response parsing,
and markdown formatting.
"""

from __future__ import annotations

from typing import Any

import pytest

from openscientist import monarch


class _FakeResponse:
    def __init__(self, payload: Any, status_ok: bool = True) -> None:
        self._payload = payload
        self._status_ok = status_ok

    def raise_for_status(self) -> None:
        if not self._status_ok:
            raise RuntimeError("HTTP error")

    def json(self) -> Any:
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the courtesy rate-limit pause in tests."""
    monkeypatch.setattr(monarch.time, "sleep", lambda _seconds: None)


def _patch_get(monkeypatch: pytest.MonkeyPatch, payload: Any) -> dict[str, Any]:
    """Patch requests.get and capture the call args."""
    captured: dict[str, Any] = {}

    def fake_get(url: str, params: dict[str, Any] | None = None, timeout: int = 0) -> _FakeResponse:
        captured["url"] = url
        captured["params"] = params
        captured["timeout"] = timeout
        return _FakeResponse(payload)

    monkeypatch.setattr(monarch.requests, "get", fake_get)
    return captured


def test_search_entities_parses_items(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "items": [
            {
                "id": "MONDO:0007947",
                "name": "Marfan syndrome",
                "category": "biolink:Disease",
                "description": "A connective tissue disorder.",
                "in_taxon_label": "Homo sapiens",
            }
        ]
    }
    captured = _patch_get(monkeypatch, payload)

    results = monarch.search_entities("Marfan syndrome", category="biolink:Disease", limit=5)

    assert captured["url"].endswith("/search")
    assert captured["params"] == {"q": "Marfan syndrome", "category": "biolink:Disease", "limit": 5}
    assert results == [
        {
            "id": "MONDO:0007947",
            "name": "Marfan syndrome",
            "category": "biolink:Disease",
            "description": "A connective tissue disorder.",
            "in_taxon_label": "Homo sapiens",
        }
    ]


def test_search_entities_drops_none_category(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _patch_get(monkeypatch, {"items": []})
    monarch.search_entities("seizure", category=None, limit=3)
    assert "category" not in captured["params"]
    assert captured["params"] == {"q": "seizure", "limit": 3}


def test_search_entities_tolerates_results_key_and_missing_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Some responses use "results"; entities may lack most fields.
    _patch_get(monkeypatch, {"results": [{"id": "HP:0001250"}, {"label": "no id"}]})
    results = monarch.search_entities("x")
    assert results[0]["id"] == "HP:0001250"
    assert results[0]["name"] == ""
    # Second item has no id/iri -> UNKNOWN sentinel, name from label.
    assert results[1]["id"] == "UNKNOWN"
    assert results[1]["name"] == "no id"


def test_search_entities_joins_list_category(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(
        monkeypatch, {"items": [{"id": "X:1", "category": ["biolink:Gene", "biolink:Thing"]}]}
    )
    results = monarch.search_entities("x")
    assert results[0]["category"] == "biolink:Gene, biolink:Thing"


def test_get_entity_normalizes_and_backfills_id(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "name": "FBN1",
        "category": "biolink:Gene",
        "synonym": ["fibrillin 1"],
        "xref": ["OMIM:134797", "HGNC:3603"],
    }
    _patch_get(monkeypatch, payload)
    entity = monarch.get_entity("HGNC:3603")
    assert entity["id"] == "HGNC:3603"  # backfilled from arg
    assert entity["synonyms"] == ["fibrillin 1"]
    assert entity["xrefs"] == ["OMIM:134797", "HGNC:3603"]


def test_get_associations_shapes_request_and_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "items": [
            {
                "subject": "MONDO:0007947",
                "subject_label": "Marfan syndrome",
                "predicate": "biolink:has_phenotype",
                "object": "HP:0002616",
                "object_label": "Aortic root aneurysm",
                "category": "biolink:DiseaseToPhenotypicFeatureAssociation",
            }
        ]
    }
    captured = _patch_get(monkeypatch, payload)
    assocs = monarch.get_associations(
        "MONDO:0007947",
        category="biolink:DiseaseToPhenotypicFeatureAssociation",
        limit=15,
    )
    assert captured["params"]["entity"] == "MONDO:0007947"
    assert captured["params"]["limit"] == 15
    assert assocs[0]["object_label"] == "Aortic root aneurysm"
    assert assocs[0]["negated"] is False


def test_get_returns_error_on_non_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_get(monkeypatch, ["not", "a", "dict"])
    with pytest.raises(monarch.MonarchError):
        monarch.search_entities("x")


def test_format_entities_markdown_empty() -> None:
    assert "No Monarch entities" in monarch.format_entities_markdown("zzz", [])


def test_format_entities_markdown_lists_curies() -> None:
    text = monarch.format_entities_markdown(
        "Marfan",
        [
            {
                "id": "MONDO:0007947",
                "name": "Marfan syndrome",
                "category": "biolink:Disease",
                "description": "desc",
                "in_taxon_label": "Homo sapiens",
            }
        ],
    )
    assert "MONDO:0007947" in text
    assert "Marfan syndrome" in text


def test_format_associations_markdown() -> None:
    text = monarch.format_associations_markdown(
        "MONDO:0007947",
        [
            {
                "subject": "MONDO:0007947",
                "subject_label": "Marfan syndrome",
                "predicate": "has_phenotype",
                "object": "HP:0002616",
                "object_label": "Aortic root aneurysm",
                "category": "",
                "negated": False,
            }
        ],
    )
    assert "has_phenotype" in text
    assert "HP:0002616" in text


def test_format_entity_markdown_includes_xrefs() -> None:
    text = monarch.format_entity_markdown(
        {
            "id": "HGNC:3603",
            "name": "FBN1",
            "category": "biolink:Gene",
            "in_taxon_label": "",
            "description": "fibrillin 1",
            "synonyms": ["FBN"],
            "xrefs": ["OMIM:134797"],
        }
    )
    assert "OMIM:134797" in text
    assert "FBN1" in text
