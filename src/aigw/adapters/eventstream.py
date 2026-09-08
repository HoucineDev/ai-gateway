"""AWS event stream framing (``application/vnd.amazon.eventstream``), used by Bedrock ``converse-stream``.

Message = prelude (total length, headers length, prelude CRC) + headers + payload + message CRC. Header values
used by Bedrock are all type 7 (string): ``:message-type`` (event | exception), ``:event-type``,
``:exception-type``, ``:content-type``. Owned implementation; the encoder exists for the mock upstream and tests.
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import AsyncIterator, Iterable

_PRELUDE = struct.Struct(">IIi")  # total_length, headers_length, prelude_crc (signed to match zlib output range)
_STRING = 7


class EventStreamError(ValueError):
    pass


def encode(headers: dict[str, str], payload: bytes) -> bytes:
    hbuf = bytearray()
    for name, value in headers.items():
        n, v = name.encode(), value.encode()
        hbuf += struct.pack(">B", len(n)) + n + struct.pack(">BH", _STRING, len(v)) + v
    total = 12 + len(hbuf) + len(payload) + 4
    prelude = struct.pack(">II", total, len(hbuf))
    out = bytearray(prelude + struct.pack(">I", zlib.crc32(prelude) & 0xFFFFFFFF))
    out += hbuf + payload
    out += struct.pack(">I", zlib.crc32(bytes(out)) & 0xFFFFFFFF)
    return bytes(out)


def decode_one(buf: bytes) -> tuple[dict[str, str], bytes, int] | None:
    """Decode the first complete message in ``buf`` → (headers, payload, consumed); None when incomplete."""
    if len(buf) < 12:
        return None
    total, hlen = struct.unpack(">II", buf[:8])
    if total < 16 or hlen > total - 16:
        raise EventStreamError(f"invalid prelude (total={total}, headers={hlen})")
    if len(buf) < total:
        return None
    if struct.unpack(">I", buf[8:12])[0] != zlib.crc32(buf[:8]) & 0xFFFFFFFF:
        raise EventStreamError("prelude CRC mismatch")
    if struct.unpack(">I", buf[total - 4 : total])[0] != zlib.crc32(buf[: total - 4]) & 0xFFFFFFFF:
        raise EventStreamError("message CRC mismatch")
    headers: dict[str, str] = {}
    pos, end = 12, 12 + hlen
    while pos < end:
        nlen = buf[pos]
        pos += 1
        name = buf[pos : pos + nlen].decode()
        pos += nlen
        htype = buf[pos]
        pos += 1
        if htype == _STRING:
            vlen = struct.unpack(">H", buf[pos : pos + 2])[0]
            pos += 2
            headers[name] = buf[pos : pos + vlen].decode()
            pos += vlen
        elif htype in (0, 1):  # bool true/false: no value bytes
            headers[name] = "true" if htype == 0 else "false"
        elif htype in (2, 3, 4, 5):  # byte, short, int, long
            size = {2: 1, 3: 2, 4: 4, 5: 8}[htype]
            headers[name] = str(int.from_bytes(buf[pos : pos + size], "big", signed=True))
            pos += size
        elif htype == 6:  # byte array
            vlen = struct.unpack(">H", buf[pos : pos + 2])[0]
            pos += 2 + vlen
            headers[name] = ""
        elif htype == 8:  # timestamp (int64 ms)
            headers[name] = str(int.from_bytes(buf[pos : pos + 8], "big", signed=True))
            pos += 8
        elif htype == 9:  # uuid (16 bytes)
            headers[name] = buf[pos : pos + 16].hex()
            pos += 16
        else:
            raise EventStreamError(f"unknown header type {htype}")
    payload = bytes(buf[end : total - 4])
    return headers, payload, total


def decode_all(data: bytes) -> list[tuple[dict[str, str], bytes]]:
    out = []
    pos = 0
    while pos < len(data):
        res = decode_one(data[pos:])
        if res is None:
            raise EventStreamError("truncated event stream")
        headers, payload, consumed = res
        out.append((headers, payload))
        pos += consumed
    return out


async def iter_messages(chunks: AsyncIterator[bytes] | Iterable[bytes]) -> AsyncIterator[tuple[dict[str, str], bytes]]:
    """Reassemble messages from arbitrary byte chunks."""
    buf = bytearray()
    if hasattr(chunks, "__aiter__"):
        async for chunk in chunks:  # type: ignore[union-attr]
            buf += chunk
            while True:
                res = decode_one(bytes(buf))
                if res is None:
                    break
                headers, payload, consumed = res
                del buf[:consumed]
                yield headers, payload
    else:
        for chunk in chunks:  # type: ignore[union-attr]
            buf += chunk
            while True:
                res = decode_one(bytes(buf))
                if res is None:
                    break
                headers, payload, consumed = res
                del buf[:consumed]
                yield headers, payload
    if buf:
        raise EventStreamError("stream ended inside a message")
