"""
Finding-Literature relationship table.

Many-to-many association between findings and literature references.
A finding can be supported by multiple literature references.
"""

from sqlalchemy import Column, ForeignKey, Table
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from ..base import Base

# Many-to-many association table between findings and literature
finding_literature = Table(
    "finding_literature",
    Base.metadata,
    Column(
        "finding_id",
        PGUUID(as_uuid=True),
        ForeignKey("findings.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Finding in this relationship",
    ),
    Column(
        "literature_id",
        PGUUID(as_uuid=True),
        ForeignKey("literature.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Literature reference in this relationship",
    ),
    comment="Many-to-many relationship between findings and literature",
)
