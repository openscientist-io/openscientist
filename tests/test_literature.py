"""Tests for literature module."""

from openscientist.literature import _parse_pubmed_xml

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
