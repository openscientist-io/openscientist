"""Tests for Phenix tool helper functions."""

from pathlib import Path

from shandy.tools.phenix import _extract_ca_confidence, _find_low_confidence_regions


def test_find_low_confidence_regions_tracks_contiguous_ranges() -> None:
    residues = [1, 2, 3, 4, 5, 6]
    plddts = [95.0, 65.0, 60.0, 80.0, 50.0, 49.0]

    regions = _find_low_confidence_regions(residues, plddts)

    assert regions == ["2-3", "5-6"]


def test_extract_ca_confidence_reads_ca_only(tmp_path: Path) -> None:
    pdb_path = tmp_path / "af_model.pdb"
    pdb_path.write_text(
        "\n".join(
            [
                "ATOM      1  N   ALA A   1      10.000  10.000  10.000  1.00 11.11           N",
                "ATOM      2  CA  ALA A   1      10.000  10.000  10.000  1.00 91.23           C",
                "ATOM      3  C   ALA A   1      10.000  10.000  10.000  1.00 12.34           C",
                "ATOM      4  CA  GLY A   2      10.000  10.000  10.000  1.00 55.00           C",
            ]
        ),
        encoding="utf-8",
    )

    residues, plddts = _extract_ca_confidence(pdb_path)

    assert residues == [1, 2]
    assert plddts == [91.23, 55.0]
