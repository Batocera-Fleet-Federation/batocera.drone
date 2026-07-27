"""Minimal multipart/form-data body parser, stdlib-only.

``http.server``'s stdlib request handling stops at raw bytes -- there is no
``cgi.FieldStorage`` equivalent worth trusting (the module is removed in
newer Pythons anyway). Extracted from the Torrents upload handler once a
second feature (VPN config upload) needed the same byte-exact parsing.
"""

from typing import List, Tuple


def parse_multipart_files(raw_body: bytes, boundary: str) -> List[Tuple[str, bytes]]:
    """Extract every file part from a multipart/form-data body as (filename, bytes).

    Byte-exact for binary payloads: only the single CRLF that belongs to the
    multipart framing is removed, never trailing bytes of the file itself.
    """
    delimiter = b"--" + boundary.encode("utf-8", errors="replace")
    files = []
    for chunk in raw_body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break  # closing delimiter
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        header_end = chunk.find(b"\r\n\r\n")
        if header_end < 0:
            continue
        headers = chunk[:header_end].decode("utf-8", errors="replace")
        body = chunk[header_end + 4 :]
        if body.endswith(b"\r\n"):
            body = body[:-2]
        disposition = next(
            (line for line in headers.split("\r\n") if line.lower().startswith("content-disposition")),
            "",
        )
        if ' filename="' not in disposition:
            continue
        filename = disposition.split(' filename="')[1].split('"')[0]
        if filename:
            files.append((filename, body))
    return files


def boundary_from_content_type(content_type: str) -> str:
    """Extract the boundary token from a multipart Content-Type header value."""
    if "boundary=" not in content_type:
        raise ValueError("boundary not found in content-type")
    boundary = content_type.split("boundary=")[1].strip()
    return boundary.strip('"').strip("'")
