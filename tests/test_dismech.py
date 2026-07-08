"""Unit tests for the DisMech client (MARDUK). Network is mocked."""

from __future__ import annotations

from typing import Any

import pytest

from openscientist import dismech

_SAMPLE = {
    "name": "Achondroplasia",
    "category": "Mendelian",
    "description": "A skeletal dysplasia caused by FGFR3 gain-of-function.",
    "disease_term": {"term": {"id": "MONDO:0007037", "label": "achondroplasia"}},
    "prevalence": [
        {
            "population": "Global live births",
            "percentage": "4.6 per 100,000",
            "notes": "Systematic review data",
        }
    ],
    "genetic": [
        {
            "name": "FGFR3 G380R mutation",
            "gene_term": {"term": {"id": "HGNC:3690", "label": "FGFR3"}},
            "description": "Recurrent activating variant.",
        }
    ],
    "phenotypes": [
        {
            "name": "Disproportionate short stature",
            "frequency": "VERY_FREQUENT",
            "phenotype_term": {"term": {"id": "HP:0003510", "label": "short stature"}},
        }
    ],
    "treatments": [{"name": "Vosoritide", "description": "CNP analogue."}],
}


class _FakeResponse:
    def __init__(self, payload: Any = None, text: str = "", status_code: int = 200) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self) -> Any:
        return self._payload


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dismech.time, "sleep", lambda _s: None)


def test_list_disorders_filters_yaml_files(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {"name": "Achondroplasia.yaml", "type": "file"},
        {"name": "Alagille_syndrome.yaml", "type": "file"},
        {"name": "README.md", "type": "file"},
        {"name": "subdir", "type": "dir"},
    ]
    monkeypatch.setattr(dismech.requests, "get", lambda *a, **k: _FakeResponse(payload=payload))
    names = dismech.list_disorders()
    assert names == ["Achondroplasia.yaml", "Alagille_syndrome.yaml"]


def test_find_disorders_case_and_underscore_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = [
        {"name": "Alagille_syndrome.yaml", "type": "file"},
        {"name": "Achondroplasia.yaml", "type": "file"},
    ]
    monkeypatch.setattr(dismech.requests, "get", lambda *a, **k: _FakeResponse(payload=payload))
    assert dismech.find_disorders("alagille syndrome") == ["Alagille_syndrome.yaml"]
    assert dismech.find_disorders("PLASIA") == ["Achondroplasia.yaml"]


def test_get_disorder_direct_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        calls.append(url)
        if url.endswith("Achondroplasia.yaml"):
            import yaml

            return _FakeResponse(text=yaml.safe_dump(_SAMPLE))
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(dismech.requests, "get", fake_get)
    data = dismech.get_disorder("Achondroplasia")
    assert data is not None
    assert data["name"] == "Achondroplasia"


def test_get_disorder_falls_back_to_fuzzy_match(monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml

    listing = [{"name": "Alagille_syndrome.yaml", "type": "file"}]

    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "api.github.com" in url:
            return _FakeResponse(payload=listing)
        if url.endswith("Alagille_syndrome.yaml"):
            return _FakeResponse(text=yaml.safe_dump({"name": "Alagille syndrome"}))
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(dismech.requests, "get", fake_get)
    # "alagille" won't match a direct candidate filename, forcing the fuzzy path.
    data = dismech.get_disorder("alagille")
    assert data is not None
    assert data["name"] == "Alagille syndrome"


def test_get_disorder_returns_none_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        if "api.github.com" in url:
            return _FakeResponse(payload=[])
        return _FakeResponse(status_code=404)

    monkeypatch.setattr(dismech.requests, "get", fake_get)
    assert dismech.get_disorder("Nonexistent Disease") is None


def test_format_disorder_markdown_all_sections() -> None:
    text = dismech.format_disorder_markdown(_SAMPLE)
    assert "Achondroplasia" in text
    assert "MONDO:0007037" in text
    assert "4.6 per 100,000" in text
    assert "FGFR3" in text
    assert "VERY_FREQUENT" in text
    assert "Vosoritide" in text


def test_format_disorder_markdown_section_filter() -> None:
    text = dismech.format_disorder_markdown(_SAMPLE, sections=["prevalence"])
    assert "Prevalence" in text
    assert "4.6 per 100,000" in text
    # Non-selected section omitted.
    assert "Vosoritide" not in text


def test_format_disorder_list_empty() -> None:
    assert "No DisMech disorders" in dismech.format_disorder_list_markdown("zzz", [])


def test_format_disorder_list_strips_extension_and_underscores() -> None:
    text = dismech.format_disorder_list_markdown("", ["Alagille_syndrome.yaml"])
    assert "Alagille syndrome" in text
