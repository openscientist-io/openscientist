"""Tests for literature module."""

from unittest.mock import MagicMock, patch

from shandy.literature import (
    _parse_pubmed_xml,
    format_literature_for_prompt,
    search_pubmed,
)

# ─── _parse_pubmed_xml ───────────────────────────────────────────────


SAMPLE_XML = """<?xml version="1.0" ?>
<PubmedArticleSet>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>12345678</PMID>
      <Article>
        <ArticleTitle>Metabolomics of hypothermia</ArticleTitle>
        <Abstract>
          <AbstractText>We found elevated carnosine levels.</AbstractText>
        </Abstract>
        <AuthorList>
          <Author><LastName>Smith</LastName></Author>
          <Author><LastName>Jones</LastName></Author>
        </AuthorList>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2024</Year></PubDate>
          </JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
  <PubmedArticle>
    <MedlineCitation>
      <PMID>87654321</PMID>
      <Article>
        <ArticleTitle>Another paper</ArticleTitle>
        <Journal>
          <JournalIssue>
            <PubDate><Year>2023</Year></PubDate>
          </JournalIssue>
        </Journal>
      </Article>
    </MedlineCitation>
  </PubmedArticle>
</PubmedArticleSet>"""


class TestParsePubmedXml:
    """Tests for PubMed XML parsing."""

    def test_parse_basic_article(self):
        papers = _parse_pubmed_xml(SAMPLE_XML, ["12345678", "87654321"])
        assert len(papers) == 2

    def test_extracts_fields(self):
        papers = _parse_pubmed_xml(SAMPLE_XML, ["12345678"])
        paper = papers[0]
        assert paper["pmid"] == "12345678"
        assert paper["title"] == "Metabolomics of hypothermia"
        assert "carnosine" in paper["abstract"]
        assert "Smith" in paper["authors"]
        assert paper["year"] == "2024"

    def test_missing_abstract(self):
        papers = _parse_pubmed_xml(SAMPLE_XML, ["87654321"])
        paper = next(p for p in papers if p["pmid"] == "87654321")
        assert "No abstract" in paper["abstract"]

    def test_malformed_xml_fallback(self):
        papers = _parse_pubmed_xml("not xml at all", ["99999"])
        assert len(papers) == 1
        assert papers[0]["pmid"] == "99999"
        assert "Error" in papers[0]["title"]

    def test_empty_xml(self):
        papers = _parse_pubmed_xml(
            '<?xml version="1.0" ?><PubmedArticleSet></PubmedArticleSet>',
            [],
        )
        assert papers == []


# ─── search_pubmed (mocked) ──────────────────────────────────────────


class TestSearchPubmed:
    """Tests for PubMed search with mocked HTTP."""

    @patch("shandy.literature.requests.get")
    @patch("shandy.literature.time.sleep")
    def test_search_and_fetch(self, mock_sleep, mock_get):
        # Mock esearch response
        search_resp = MagicMock()
        search_resp.json.return_value = {"esearchresult": {"idlist": ["12345678"]}}
        search_resp.raise_for_status = MagicMock()

        # Mock efetch response
        fetch_resp = MagicMock()
        fetch_resp.text = SAMPLE_XML
        fetch_resp.raise_for_status = MagicMock()

        mock_get.side_effect = [search_resp, fetch_resp]

        papers = search_pubmed("hypothermia metabolomics", max_results=5)
        assert len(papers) >= 1
        assert papers[0]["pmid"] == "12345678"

    @patch("shandy.literature.requests.get")
    def test_no_results_returns_empty(self, mock_get):
        resp = MagicMock()
        resp.json.return_value = {"esearchresult": {"idlist": []}}
        resp.raise_for_status = MagicMock()
        mock_get.return_value = resp

        papers = search_pubmed("nonexistent query xyz")
        assert papers == []


# ─── format_literature_for_prompt ─────────────────────────────────────


class TestFormatLiteratureForPrompt:
    """Tests for literature formatting."""

    def test_no_papers(self):
        result = format_literature_for_prompt([])
        assert "No literature" in result

    def test_formats_papers(self):
        papers = [
            {"pmid": "111", "title": "Paper A about things", "year": "2024"},
            {"pmid": "222", "title": "Paper B about stuff", "year": "2023"},
        ]
        result = format_literature_for_prompt(papers)
        assert "PMID 111" in result
        assert "Paper A" in result
        assert "2024" in result

    def test_respects_max_papers(self):
        papers = [{"pmid": str(i), "title": f"Paper {i}", "year": "2024"} for i in range(10)]
        result = format_literature_for_prompt(papers, max_papers=3)
        assert result.count("PMID") == 3
