"""Bounded request body reads for internal listeners."""

from starlette.exceptions import HTTPException
from starlette.requests import Request


async def read_request_body(request: Request, *, limit: int) -> bytes:
    """Read at most limit bytes, rejecting oversized bodies before buffering them."""
    content_length = request.headers.get("content-length")
    if content_length is not None:
        if not content_length.isascii() or not content_length.isdecimal():
            raise HTTPException(400, "Invalid Content-Length")
        length = content_length.lstrip("0") or "0"
        maximum = str(limit)
        if len(length) > len(maximum) or (len(length) == len(maximum) and length > maximum):
            raise HTTPException(413, "Request body too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(chunk) > limit - len(body):
            raise HTTPException(413, "Request body too large")
        body.extend(chunk)
    return bytes(body)
