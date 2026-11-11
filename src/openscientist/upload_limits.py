"""Bound upload bodies before downstream multipart spooling."""

import re

from python_multipart import MultipartParser
from python_multipart.multipart import parse_options_header
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from openscientist.admin_settings import current_admin_settings

MAX_UPLOAD_FILES_PER_REQUEST = 100
MAX_FORM_OVERHEAD_BYTES = 1024 * 1024
_NICEGUI_UPLOAD = re.compile(r"/_nicegui/client/[^/]+/upload/[^/]+/?")


class _MultipartLimits:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.files = 0
        self.total = 0
        self.is_file = False
        self.header_name = bytearray()
        self.header_value = bytearray()

    def on_part_begin(self) -> None:
        self.is_file = False

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self.header_name.extend(data[start:end])

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self.header_value.extend(data[start:end])

    def on_header_end(self) -> None:
        if self.header_name.lower() == b"content-disposition":
            _, options = parse_options_header(bytes(self.header_value))
            self.is_file = b"filename" in options
        self.header_name.clear()
        self.header_value.clear()

    def on_headers_finished(self) -> None:
        if self.is_file:
            self.files += 1
            if self.files > MAX_UPLOAD_FILES_PER_REQUEST:
                raise HTTPException(413, "Too many uploaded files")

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self.is_file:
            self.total += end - start
            if self.total > self.limit:
                raise HTTPException(413, "Uploaded files exceed the size limit")


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

        content_type, options = parse_options_header(headers.get("content-type", ""))
        parser = None
        if content_type == b"multipart/form-data" and b"boundary" in options:
            limits = _MultipartLimits(limit)
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
        rejection: str | None = None

        async def limited_receive() -> Message:
            nonlocal received, rejection
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                received += len(body)
                if received > body_limit:
                    rejection = "Upload body exceeds the size limit"
                    raise HTTPException(413, rejection)
                if parser is not None:
                    try:
                        parser.write(body)
                    except HTTPException as error:
                        rejection = str(error.detail)
                        raise
            return message

        async def limited_send(message: Message) -> None:
            if rejection is None:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except Exception:
            if rejection is None:
                raise
        if rejection is not None:
            await JSONResponse({"detail": rejection}, status_code=413)(scope, receive, send)
