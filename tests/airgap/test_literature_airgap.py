"""Sentinel for the PUBMED_BASE_URL env-var respect in :func:`openscientist.literature.search_pubmed`.

Codex Review-6 BUG (fixed): the base_url was hardcoded so
``OPENSCIENTIST_AIRGAP_PUBMED_ADDR`` (and the documented ``PUBMED_BASE_URL``
override) had no effect — air-gap deployments would have silently hit
public NCBI eutils. This test pins the env-var read.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from openscientist.literature import search_pubmed


@pytest.fixture
def mock_requests():
    """Patch ``requests.get`` so the test never hits the network."""
    with (
        patch("openscientist.literature.requests.get") as mock_get,
        patch("openscientist.literature.time.sleep"),
    ):
        # esearch returns a known PMID list shape; efetch returns minimal XML
        # so the parser returns no papers (we only care about the URL the
        # search call hit, not the response content).
        esearch_resp = MagicMock()
        esearch_resp.json.return_value = {"esearchresult": {"idlist": ["1"]}}
        esearch_resp.raise_for_status = MagicMock()

        efetch_resp = MagicMock()
        efetch_resp.text = "<?xml version='1.0'?><PubmedArticleSet></PubmedArticleSet>"
        efetch_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [esearch_resp, efetch_resp]
        yield mock_get


class TestPubmedBaseUrlOverride:
    def test_default_uses_ncbi_eutils(
        self,
        mock_requests: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PUBMED_BASE_URL", raising=False)
        search_pubmed("test", max_results=1)
        first_url = mock_requests.call_args_list[0].args[0]
        assert first_url.startswith("https://eutils.ncbi.nlm.nih.gov/entrez/eutils")

    def test_env_var_redirects_to_internal_mirror(
        self,
        mock_requests: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # The case the Codex Review-6 bug broke. With PUBMED_BASE_URL set,
        # search_pubmed must hit THAT host, not NCBI.
        monkeypatch.setenv("PUBMED_BASE_URL", "http://10.0.0.6:9000")
        search_pubmed("test", max_results=1)
        first_url = mock_requests.call_args_list[0].args[0]
        assert first_url.startswith("http://10.0.0.6:9000/esearch.fcgi")
        assert "ncbi.nlm.nih.gov" not in first_url

    def test_trailing_slash_in_env_var_normalized(
        self,
        mock_requests: MagicMock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Defensive — operator-set base URL with trailing slash should not
        # double-up the separator and produce a malformed eutils URL.
        monkeypatch.setenv("PUBMED_BASE_URL", "http://mirror/")
        search_pubmed("test", max_results=1)
        first_url = mock_requests.call_args_list[0].args[0]
        assert first_url == "http://mirror/esearch.fcgi"
