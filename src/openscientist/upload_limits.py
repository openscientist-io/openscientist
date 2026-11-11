"""Bound upload bodies before downstream multipart spooling."""

import re

from nicegui.client import Client
from nicegui.elements.upload_files import _sanitize_filename
from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.formparsers import _user_safe_decode
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from openscientist.admin_settings import current_admin_settings
from openscientist.webapp_components.utils.session import (
    PendingUpload,
    check_upload_quota,
    finish_upload,
    reserve_upload,
)

MAX_UPLOAD_FILES_PER_REQUEST = 100
MAX_FORM_OVERHEAD_BYTES = 1024 * 1024
_NICEGUI_UPLOAD = re.compile(
    r"/_nicegui/client/(?P<client_id>[^/]+)/upload/(?P<upload_id>[0-9]+)/?"
)


class _MultipartLimits:
    def __init__(self, limit: int, session_id: str | None, charset: str, max_files: int) -> None:
        self.limit = limit
        self.files = 0
        self.total = 0
        self.is_file = False
        self.header_name = bytearray()
        self.header_value = bytearray()
        self.filename: bytes | None = None
        self.session_id = session_id
        self.charset = charset
        self.reservations: list[PendingUpload] = []
        self.current_upload: PendingUpload | None = None
        self.max_files = max_files

    def on_part_begin(self) -> None:
        self.is_file = False
        self.filename = None
        self.current_upload = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_name.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        if self.header_name.lower() == b"content-disposition":
            _, options = parse_options_header(bytes(self.header_value))
            self.is_file = b"filename" in options
            self.filename = options.get(b"filename")
        self.header_name.clear()
        self.header_value.clear()

    def on_headers_finished(self) -> None:
        if self.is_file:
            self.files += 1
            if self.files > MAX_UPLOAD_FILES_PER_REQUEST:
                raise HTTPException(413, "Too many uploaded files")
            if self.session_id is not None and self.filename is not None:
                name = _sanitize_filename(_user_safe_decode(self.filename, self.charset))
                try:
                    self.current_upload = reserve_upload(self.session_id, name)
                    self.reservations.append(self.current_upload)
                except ValueError as error:
                    raise HTTPException(409, str(error)) from error
                self.check_page_quota()

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self.is_file:
            self.total += end - start
            if self.total > self.limit:
                raise HTTPException(413, "Uploaded files exceed the size limit")
            if self.current_upload is not None:
                self.current_upload.size += end - start
                self.check_page_quota()

    def check_page_quota(self) -> None:
        if self.session_id is not None:
            try:
                check_upload_quota(self.session_id, self.max_files, self.limit)
            except ValueError as error:
                raise HTTPException(413, str(error)) from error

    def release_unclaimed(self) -> None:
        if self.session_id is not None:
            for upload in self.reservations:
                if not upload.claimed:
                    finish_upload(self.session_id, upload)


class UploadLimitsMiddleware:
    """Limit file count, file bytes and request bytes on upload routes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not (
                scope["path"].rstrip("/") == "/api/v1/jobs"
                or _NICEGUI_UPLOAD.fullmatch(scope["path"])
            )
        ):
            await self.app(scope, receive, send)
            return

        limit = (await current_admin_settings()).max_file_size_mb * 1024 * 1024
        body_limit = limit + MAX_FORM_OVERHEAD_BYTES
        headers = Headers(scope=scope)
        content_length = headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)(
                    scope, receive, send
                )
                return
            if declared_size > body_limit:
                await JSONResponse(
                    {"detail": "Upload body exceeds the size limit"}, status_code=413
                )(scope, receive, send)
                return

        session_id: str | None = None
        max_files = MAX_UPLOAD_FILES_PER_REQUEST
        upload_match = _NICEGUI_UPLOAD.fullmatch(scope["path"])
        if upload_match is not None:
            client = Client.instances.get(upload_match["client_id"])
            if client is not None:
                try:
                    element = client.elements.get(int(upload_match["upload_id"]))
                except ValueError:
                    element = None
                candidate = getattr(element, "_upload_session_id", None)
                if isinstance(candidate, str):
                    session_id = candidate
                    if element is not None:
                        page_max_files = element.props.get("max-files")
                        if isinstance(page_max_files, int):
                            max_files = page_max_files

        content_type, options = parse_options_header(headers.get("content-type", ""))
        parser = None
        limits = None
        if content_type == b"multipart/form-data" and b"boundary" in options:
            charset = options.get(b"charset", b"utf-8").decode("latin-1")
            limits = _MultipartLimits(limit, session_id, charset, max_files)
            parser = MultipartParser(
                options[b"boundary"],
                {
                    "on_part_begin": limits.on_part_begin,
                    "on_header_field": limits.on_header_field,
                    "on_header_value": limits.on_header_value,
                    "on_header_end": limits.on_header_end,
                    "on_headers_finished": limits.on_headers_finished,
                    "on_part_data": limits.on_part_data,
                },
            )
        received = 0
        rejection: HTTPException | None = None

        async def limited_receive() -> Message:
            nonlocal received, rejection
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                received += len(body)
                if received > body_limit:
                    rejection = HTTPException(413, "Upload body exceeds the size limit")
                    raise rejection
                if parser is not None:
                    try:
                        parser.write(body)
                    except HTTPException as error:
                        rejection = error
                        raise
            return message

        async def limited_send(message: Message) -> None:
            if rejection is None:
                await send(message)

        try:
            try:
                await self.app(scope, limited_receive, limited_send)
            except Exception:
                if rejection is None:
                    raise
            if rejection is not None:
                await JSONResponse({"detail": rejection.detail}, status_code=rejection.status_code)(
                    scope, receive, send
                )
        finally:
            if limits is not None:
                limits.release_unclaimed()
