"""Finding-Hypothesis relationship table."""

from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from ..base import Base

finding_hypotheses = Table(
    "finding_hypotheses",
    Base.metadata,
    Column(
        "finding_id",
        PGUUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Finding in this relationship",
    ),
    Column(
        "hypothesis_id",
        PGUUID(as_uuid=True),
        ForeignKey("hypotheses.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Hypothesis in this relationship",
    ),
    comment="Many-to-many relationship between findings and hypotheses",
)
