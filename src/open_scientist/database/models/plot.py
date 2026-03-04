"""
Plot model for generated visualizations.

Tracks metadata for plots generated during analysis. Plot image files
remain on filesystem; only metadata stored in database.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class Plot(UUIDv7Mixin, Base):
    """
    Generated plot/visualization.

    Tracks metadata for plots generated during analysis. The actual
    plot image is stored on the filesystem, referenced by file_path.

    Attributes:
        job_id: Foreign key to the job this plot belongs to
        iteration: Iteration number when plot was generated
        title: Plot title
        description: Description of what the plot shows
        file_path: Relative path to plot image file
        plot_type: Type of plot (histogram/scatter/heatmap/etc)
        code_used: Python code that generated the plot
        job: Related Job object
    """

    __tablename__ = "plots"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this plot belongs to",
    )

    iteration: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Iteration number when plot was generated",
    )

    title: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Plot title",
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Description of what the plot shows",
    )

    file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Relative path to plot image file",
    )

    plot_type: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Type of plot (histogram/scatter/heatmap/etc)",
    )

    code_used: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Python code that generated the plot",
    )

    # Relationships
    job: Mapped["Job"] = relationship()

    def __repr__(self) -> str:
        return f"<Plot(id={self.id}, title={self.title}, iteration={self.iteration})>"
