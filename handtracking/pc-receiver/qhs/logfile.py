"""Журнал .qhslog: исходные пакеты с метками времени (формат одинаков на ПК и Quest)."""
from __future__ import annotations

import json
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO, Iterator

LOG_MAGIC = b"QHSLOG01"
RECORD = struct.Struct("<BqH")  # dir, t_ns, len
DIR_RECEIVED = 0
DIR_SENT = 1


@dataclass
class Record:
    direction: int
    t_ns: int
    data: bytes


class LogWriter:
    def __init__(self, path: str, metadata: dict):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.path = path
        self._f: BinaryIO = open(path, "xb", buffering=1 << 16)
        meta = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
        self._f.write(LOG_MAGIC + struct.pack("<I", len(meta)) + meta)
        self.bytes_written = len(LOG_MAGIC) + 4 + len(meta)
        self.records = 0

    def write(self, direction: int, t_ns: int, data: bytes) -> None:
        self._f.write(RECORD.pack(direction, t_ns, len(data)))
        self._f.write(data)
        self.bytes_written += RECORD.size + len(data)
        self.records += 1

    def flush(self) -> None:
        self._f.flush()

    def close(self) -> None:
        if not self._f.closed:
            self._f.flush()
            os.fsync(self._f.fileno())
            self._f.close()


def read_log(path: str) -> tuple[dict, Iterator[Record]]:
    f = open(path, "rb")
    magic = f.read(len(LOG_MAGIC))
    if magic != LOG_MAGIC:
        f.close()
        raise ValueError(f"{path}: not a qhslog file")
    (n,) = struct.unpack("<I", f.read(4))
    meta = json.loads(f.read(n).decode("utf-8"))

    def records() -> Iterator[Record]:
        with f:
            while True:
                hdr = f.read(RECORD.size)
                if len(hdr) < RECORD.size:
                    return  # конец файла (в т.ч. оборванная последняя запись)
                d, t, length = RECORD.unpack(hdr)
                data = f.read(length)
                if len(data) < length:
                    return
                yield Record(d, t, data)

    return meta, records()
