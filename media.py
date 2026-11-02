"""Best-effort video duration reading without any native dependency.

For MP4 files we walk the box tree to the mvhd box and read
timescale/duration. For anything else we return None and the caller falls
back to the duration the browser read from the file before upload.
Production should verify durations server-side with a media probe tool;
this keeps the first version dependency-free on purpose.
"""

import struct


def _u32(data, off):
    return struct.unpack_from(">I", data, off)[0]


def _u64(data, off):
    return struct.unpack_from(">Q", data, off)[0]


def _walk_boxes(data, start, end):
    """Yield (box_type, payload_start, payload_end) for boxes in a range."""
    off = start
    while off + 8 <= end:
        size = _u32(data, off)
        btype = data[off + 4:off + 8]
        if size == 1:
            if off + 16 > end:
                return
            size = _u64(data, off + 8)
            header = 16
        elif size == 0:
            size = end - off
            header = 8
        else:
            header = 8
        if size < header or off + size > end:
            return
        yield btype, off + header, off + size
        off += size


def parse_mp4_duration(path):
    """Return duration in seconds for an MP4 file, or None if unreadable."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    try:
        for btype, start, end in _walk_boxes(data, 0, len(data)):
            if btype != b"moov":
                continue
            for inner_type, istart, iend in _walk_boxes(data, start, end):
                if inner_type != b"mvhd":
                    continue
                payload = data[istart:iend]
                if len(payload) < 4:
                    return None
                version = payload[0]
                if version == 0:
                    if len(payload) < 20:
                        return None
                    timescale = _u32(payload, 12)
                    duration = _u32(payload, 16)
                elif version == 1:
                    if len(payload) < 32:
                        return None
                    timescale = _u32(payload, 20)
                    duration = _u64(payload, 24)
                else:
                    return None
                if timescale == 0:
                    return None
                return duration / timescale
    except (struct.error, IndexError):
        return None
    return None
