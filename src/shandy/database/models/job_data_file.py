"""
Job data file model for uploaded crystallography data.

Tracks uploaded data files (MTZ, CIF, PDB, etc.) with metadata.
Binary file contents remain on filesystem; only metadata stored in DB.
"""

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..base import Base, UUIDv7Mixin

if TYPE_CHECKING:
    from .job import Job


class JobDataFile(UUIDv7Mixin, Base):
    """
    Uploaded data file for a job.

    Tracks metadata for uploaded crystallography data files. The actual
    binary content is stored on the filesystem, referenced by file_path.

    Attributes:
        job_id: Foreign key to the job this file belongs to
        filename: Original filename as uploaded by user
        file_path: Relative path to file on filesystem
        file_type: Type of file (mtz/cif/pdb/other)
        file_size: File size in bytes
        mime_type: MIME type of the file
        job: Related Job object
    """

    __tablename__ = "job_data_files"

    job_id: Mapped[UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("jobs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Job this file belongs to",
    )

    filename: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Original filename as uploaded by user",
    )

    file_path: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="Relative path to file on filesystem",
    )

    file_type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="Type of file (mtz/cif/pdb/other)",
    )

    file_size: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="File size in bytes",
    )

    mime_type: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        comment="MIME type of the file",
    )

    # Relationships
    job: Mapped["Job"] = relationship(back_populates="data_files")

    def __repr__(self) -> str:
        return f"<JobDataFile(id={self.id}, filename={self.filename}, job_id={self.job_id})>"
