"""Symlink-safe access to files in the agent-writable job directories.

The job directory is bind-mounted read-write into the agent container, so
any entry in it can be replaced by a symlink pointing outside the directory
while the web process runs as root with the admin database password and
provider keys in its environment. Nothing in the web process may follow
such a link. Every read, existence check, listing and write here opens the
job directory and then walks each path component with os.open and
O_NOFOLLOW, so a planted link is refused at any level, and a file opened
through here stays the regular file it was at the moment of the open.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import AsyncIterator, Mapping
from email.utils import formatdate
from hashlib import md5
from mimetypes import guess_type
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import anyio
from starlette.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from openscientist.job_container.identity import JOB_FILE_MODE

_CHUNK_BYTES = 64 * 1024
_WRITE_ATTEMPTS = 8


class JobFileError(OSError):
    """A job-directory path is missing, escapes the directory, or resolves
    through a symlink."""


class JobFile:
    """A handle on a regular file in a job directory, pinned by its fd.

    The path may change afterwards (renamed, or replaced by a symlink); the
    handle keeps pointing at the inode that was opened, so serving or
    reading it cannot be diverted.
    """

    def __init__(self, fd: int, path: Path, stat_result: os.stat_result) -> None:
        self._fd = fd
        self._closed = False
        self.path = path
        self.stat = stat_result

    @property
    def size(self) -> int:
        return self.stat.st_size

    def read_all(self) -> bytes:
        data = bytearray()
        while chunk := os.read(self._fd, _CHUNK_BYTES):
            data += chunk
        return bytes(data)

    def close(self) -> None:
        if not self._closed:
            os.close(self._fd)
            self._closed = True

    def __enter__(self) -> JobFile:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _validate_parts(
    relative: str | os.PathLike[str], *, allow_empty: bool = False
) -> tuple[str, ...]:
    """The components of ``relative``; absolute paths, backslashes, ``.``,
    ``..`` and (unless ``allow_empty``) the empty path are refused."""
    text = os.fspath(relative)
    if "\\" in text or text.startswith("/"):
        raise JobFileError(f"unsafe job file path: {text!r}")
    parts = PurePosixPath(text).parts
    if (not parts and not allow_empty) or any(part in (".", "..") for part in parts):
        raise JobFileError(f"unsafe job file path: {text!r}")
    return parts


def _open_walk(
    job_dir: Path, parts: tuple[str, ...], *, leaf_is_dir: bool
) -> tuple[int, os.stat_result]:
    """Open ``job_dir`` joined with ``parts`` (or ``job_dir`` itself for empty
    parts), following no symlink at any level. Returns the fd and its fstat;
    JobFileError on any failure."""
    # O_NONBLOCK keeps a planted FIFO from hanging the caller: the open
    # returns and the S_ISREG check below rejects the entry.
    flags = os.O_RDONLY | os.O_NONBLOCK | (os.O_DIRECTORY if leaf_is_dir else 0)
    open_fds: list[int] = []
    try:
        open_fds.append(os.open(os.fspath(job_dir), flags))
        for part in parts[:-1]:
            fd = os.open(
                part,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_NONBLOCK,
                dir_fd=open_fds[-1],
            )
            os.close(open_fds.pop())
            open_fds.append(fd)
        if parts:
            fd = os.open(parts[-1], flags | os.O_NOFOLLOW, dir_fd=open_fds[-1])
        else:
            fd = open_fds.pop()
        for still_open in open_fds:
            os.close(still_open)
    except OSError as exc:
        for still_open in open_fds:
            try:
                os.close(still_open)
            except OSError:
                pass
        raise JobFileError(f"cannot open {job_dir.joinpath(*parts)}: {exc}") from exc
    stat_result = os.fstat(fd)
    is_dir = stat.S_ISDIR(stat_result.st_mode)
    is_regular = stat.S_ISREG(stat_result.st_mode)
    if not (is_dir if leaf_is_dir else is_regular):
        os.close(fd)
        expected = "a directory" if leaf_is_dir else "a regular file"
        raise JobFileError(f"not {expected}: {job_dir.joinpath(*parts)}")
    return fd, stat_result


def open_job_file(
    job_dir: Path,
    relative: str | os.PathLike[str],
    *,
    max_size: int | None = None,
) -> JobFile:
    """Open the regular file at ``relative`` in ``job_dir`` without following
    symlinks. Every component must be a real directory, the leaf a real file;
    a file larger than ``max_size`` (when given) is refused."""
    parts = _validate_parts(relative)
    fd, stat_result = _open_walk(job_dir, parts, leaf_is_dir=False)
    if max_size is not None and stat_result.st_size > max_size:
        os.close(fd)
        raise JobFileError(f"{relative} exceeds the size cap")
    return JobFile(fd, job_dir.joinpath(*parts), stat_result)


def try_open_job_file(
    job_dir: Path,
    relative: str | os.PathLike[str],
    *,
    max_size: int | None = None,
) -> JobFile | None:
    """open_job_file, or None when the path is missing, unsafe or resolves
    through a symlink."""
    try:
        return open_job_file(job_dir, relative, max_size=max_size)
    except JobFileError:
        return None


def read_job_file_bytes(
    job_dir: Path,
    relative: str | os.PathLike[str],
    *,
    max_size: int | None = None,
) -> bytes | None:
    """The bytes of the file at ``relative``, or None when the path is
    missing, unsafe or resolves through a symlink."""
    try:
        with open_job_file(job_dir, relative, max_size=max_size) as job_file:
            return job_file.read_all()
    except JobFileError:
        return None


def read_job_file_text(
    job_dir: Path,
    relative: str | os.PathLike[str],
    *,
    max_size: int | None = None,
) -> str | None:
    """read_job_file_bytes decoded as UTF-8, or None when it cannot be read."""
    data = read_job_file_bytes(job_dir, relative, max_size=max_size)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def job_file_exists(job_dir: Path, relative: str | os.PathLike[str]) -> bool:
    """Whether ``relative`` is a regular file reachable without following any
    symlink."""
    try:
        job_file = open_job_file(job_dir, relative)
    except JobFileError:
        return False
    job_file.close()
    return True


def list_job_files(job_dir: Path, relative: str | os.PathLike[str]) -> list[str]:
    """The names of the regular files directly inside the directory at
    ``relative`` (the job directory itself for the empty path). Symlinks are
    never listed, and a missing or linked directory yields []."""
    parts = _validate_parts(relative, allow_empty=True)
    try:
        dir_fd, _ = _open_walk(job_dir, parts, leaf_is_dir=True)
    except JobFileError:
        return []
    try:
        return sorted(
            entry.name for entry in os.scandir(dir_fd) if entry.is_file(follow_symlinks=False)
        )
    finally:
        os.close(dir_fd)


def _create_and_write_fd(dir_fd: int, name: str, payload: bytes) -> None:
    """Create ``name`` fresh (refusing a planted entry) and write payload,
    addressing ``dir_fd`` so no parent of ``name`` is path-resolved."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK
    try:
        fd = os.open(name, flags, 0o644, dir_fd=dir_fd)
    except FileExistsError:
        # A planted entry at the staged name; unlink removes the entry, not
        # its target, so a planted link cannot redirect the write.
        os.unlink(name, dir_fd=dir_fd)
        fd = os.open(name, flags, 0o644, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


def _staged_write(job_dir: Path, parts: tuple[str, ...], payload: bytes, label: str) -> None:
    """Stage a file in the walked parent directory and rename it over the
    leaf. Both operations address the pinned parent fd, so no path component
    of the destination is ever resolved and a symlinked parent cannot
    redirect the write. The staged file is chmodded before the rename, so
    the umask never leaves a job file unreadable to the agent's group."""
    parent_fd, _ = _open_walk(job_dir, parts[:-1], leaf_is_dir=True)
    try:
        for _ in range(_WRITE_ATTEMPTS):
            staged = f".{parts[-1]}.{secrets.token_hex(8)}.tmp"
            try:
                _create_and_write_fd(parent_fd, staged, payload)
            except OSError as exc:
                raise JobFileError(f"cannot stage write for {label}: {exc}") from exc
            os.chmod(staged, JOB_FILE_MODE, dir_fd=parent_fd)
            try:
                os.replace(staged, parts[-1], src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                return
            except OSError as exc:
                try:
                    os.unlink(staged, dir_fd=parent_fd)
                except OSError:
                    pass
                if exc.errno == errno.ENOENT:
                    continue
                raise JobFileError(f"cannot write {label}: {exc}") from exc
        raise JobFileError(f"cannot write {label}: persistent link race")
    finally:
        os.close(parent_fd)


def write_job_file(job_dir: Path, relative: str | os.PathLike[str], data: str | bytes) -> None:
    """Write ``data`` to the file at ``relative``.

    A symlink planted at the name is replaced by a regular file, and the
    target of a planted link is never written through. A persistent link
    race raises JobFileError.
    """
    parts = _validate_parts(relative)
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    _staged_write(job_dir, parts, payload, str(job_dir.joinpath(*parts)))


def install_file(job_dir: Path, relative: str | os.PathLike[str], source: Path) -> None:
    """Move ``source`` into place at ``relative`` in ``job_dir``, replacing
    any entry there (a planted link included) without following any path
    component of the destination."""
    write_job_file(job_dir, relative, source.read_bytes())


class _JobFileResponse(StreamingResponse):
    """Streams an opened job file's fd to the client; the path behind the fd
    is never reopened."""

    chunk_size = _CHUNK_BYTES

    def __init__(self, job_file: JobFile, media_type: str, headers: dict[str, str]) -> None:
        async def stream() -> AsyncIterator[bytes]:
            fd = job_file._fd  # noqa: SLF001
            try:
                while True:
                    chunk = await anyio.to_thread.run_sync(os.read, fd, self.chunk_size)
                    if not chunk:
                        return
                    yield chunk
            finally:
                job_file.close()

        super().__init__(stream(), media_type=media_type, headers=headers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"].upper() == "HEAD":
            await send(
                {
                    "type": "http.response.start",
                    "status": self.status_code,
                    "headers": self.raw_headers,
                }
            )
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return
        await super().__call__(scope, receive, send)


def serve_job_file(
    job_file: JobFile,
    *,
    media_type: str | None = None,
    filename: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    """A streaming HTTP response over the fd ``job_file`` was opened on.

    The path behind the fd is never reopened, so the response cannot serve a
    symlink the agent plants after the open. Sets the same headers a
    FileResponse would: content-length, last-modified, etag, and
    content-disposition for ``filename``; ``media_type`` is guessed from the
    name when omitted, as FileResponse does.
    """
    if media_type is None:
        media_type = guess_type(filename or job_file.path.name)[0] or "application/octet-stream"
    response_headers: dict[str, str] = dict(headers or {})
    stat_result = job_file.stat
    if filename is not None:
        quoted = quote(filename)
        if quoted != filename:
            disposition = f"attachment; filename*=utf-8''{quoted}"
        else:
            disposition = f'attachment; filename="{filename}"'
        response_headers.setdefault("content-disposition", disposition)
    response_headers.setdefault("content-length", str(stat_result.st_size))
    response_headers.setdefault("last-modified", formatdate(stat_result.st_mtime, usegmt=True))
    etag_base = f"{stat_result.st_mtime}-{stat_result.st_size}"
    etag = f'"{md5(etag_base.encode(), usedforsecurity=False).hexdigest()}"'
    response_headers.setdefault("etag", etag)
    return _JobFileResponse(job_file, media_type=media_type, headers=response_headers)
