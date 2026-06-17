"""Fetch a small topical corpus from NCBI eutils for the airgap mock.

Run once on a machine with internet access. Saves abstracts as JSON in
``../corpus/`` for the mock server to serve back to airgap agents.

The corpus targets the research areas the agent is most likely to query
in our smoke tests: torpor, hibernation, hypothermia, preoptic area /
KOR signaling, brain metabolomics. Each topic gets ~50-100 abstracts.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"
EMAIL = "openscientist-airgap-mock@example.com"

# Each topic is a search term. We pull `MAX_PER_TOPIC` ids per topic and
# fetch their abstracts. Overlap across topics is fine — we dedupe on PMID.
TOPICS = [
    "torpor",
    "mammalian hibernation",
    "therapeutic hypothermia neuroprotection",
    "preoptic area thermoregulation",
    "kappa opioid receptor hypothermia",
    "brain metabolomics",
    "fatty acid oxidation brain",
    "citric acid cycle hypoxia",
    "hypometabolic state cerebroprotection",
    "chemogenetic activation neurons body temperature",
]
MAX_PER_TOPIC = 50


def esearch(term: str, retmax: int = MAX_PER_TOPIC) -> list[str]:
    r = requests.get(
        f"{EUTILS}/esearch.fcgi",
        params={
            "db": "pubmed",
            "term": term,
            "retmax": retmax,
            "retmode": "json",
            "email": EMAIL,
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("esearchresult", {}).get("idlist", [])


def efetch(pmids: list[str]) -> str:
    r = requests.get(
        f"{EUTILS}/efetch.fcgi",
        params={
            "db": "pubmed",
            "id": ",".join(pmids),
            "retmode": "xml",
            "email": EMAIL,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.text


def main() -> None:
    out_dir = Path(__file__).resolve().parent.parent / "corpus"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: id resolution
    pmids_per_topic: dict[str, list[str]] = {}
    seen: set[str] = set()
    for topic in TOPICS:
        try:
            ids = esearch(topic)
        except Exception as exc:
            print(f"esearch failed for {topic!r}: {exc}", file=sys.stderr)
            continue
        fresh = [p for p in ids if p not in seen]
        seen.update(fresh)
        pmids_per_topic[topic] = ids
        print(f"  {topic!r}: {len(ids)} hits ({len(fresh)} new)")
        time.sleep(0.34)

    # Save id index — used by /esearch.fcgi to answer queries
    (out_dir / "topic_index.json").write_text(
        json.dumps(pmids_per_topic, indent=2, sort_keys=True)
    )

    # Phase 2: bulk efetch all unique ids in batches
    all_ids = sorted(seen)
    print(f"\nFetching abstracts for {len(all_ids)} unique PMIDs...")
    batch_size = 100
    abstracts_xml: list[str] = []
    for i in range(0, len(all_ids), batch_size):
        batch = all_ids[i : i + batch_size]
        try:
            xml = efetch(batch)
            abstracts_xml.append(xml)
            print(f"  batch {i // batch_size + 1}: {len(batch)} abstracts")
        except Exception as exc:
            print(f"efetch batch {i // batch_size + 1} failed: {exc}", file=sys.stderr)
        time.sleep(0.34)

    # Save raw XML (the mock will index-grep on it for efetch.fcgi responses)
    (out_dir / "abstracts.xml").write_text("\n".join(abstracts_xml))

    # Tiny manifest for the mock server
    (out_dir / "manifest.json").write_text(
        json.dumps(
            {
                "total_unique_pmids": len(all_ids),
                "topics": list(pmids_per_topic.keys()),
                "fetched_at_unix": int(time.time()),
            },
            indent=2,
        )
    )
    print(f"\nDone. Wrote corpus to {out_dir}")


if __name__ == "__main__":
    main()
