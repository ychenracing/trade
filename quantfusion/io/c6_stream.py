"""Bounded record IO for C6 evidence; no engine or candidate imports.

Large arrays hold byte offsets into private files, never decoded record caches.
The wire format stays ordinary canonical JSON, including its final newline.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
import zipfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, BinaryIO, TextIO, overload

RECORD_LIMIT = 64 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
ARCHIVE_LIMIT = 4 * 1024 ** 3


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _finite(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


_DECODER = json.JSONDecoder(object_pairs_hook=_unique, parse_float=_finite, parse_constant=_finite)


class FileArray(Sequence[Any]):
    """Read-only JSON array view. Files must stay immutable for the view's lifetime."""

    def __init__(self, path: Path, spans: Sequence[tuple[int, int]], *,
                 field: str | None = None, owner: Any = None) -> None:
        self.path, self.spans, self.field, self.owner = path, list(spans), field, owner

    def __len__(self) -> int:
        return len(self.spans)

    @overload
    def __getitem__(self, key: int) -> Any: ...

    @overload
    def __getitem__(self, key: slice) -> FileArray: ...

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            return FileArray(self.path, self.spans[key], field=self.field, owner=self.owner)
        with self.path.open("rb") as stream:
            return self._read(stream, self.spans[key])

    def _read(self, stream: BinaryIO, span: tuple[int, int]) -> Any:
        offset, size = span
        stream.seek(offset)
        raw = stream.read(size)
        if len(raw) != size:
            raise ValueError("indexed JSON record was truncated")
        value = _DECODER.decode(raw.decode("utf-8"))
        return value if self.field is None else value[self.field]

    def __iter__(self) -> Iterator[Any]:
        with self.path.open("rb") as stream:
            for span in self.spans:
                yield self._read(stream, span)

    def select(self, predicate: Callable[[Any], bool]) -> FileArray:
        return FileArray(self.path, [span for span, item in zip(self.spans, self) if predicate(item)], field=self.field, owner=self.owner)

    def project(self, field: str) -> FileArray:
        if self.field is not None:
            raise ValueError("record view already projected")
        return FileArray(self.path, self.spans, field=field, owner=self.owner)


def select_records(records: Sequence[Any], predicate: Callable[[Any], bool]) -> Sequence[Any]:
    return records.select(predicate) if isinstance(records, FileArray) else [item for item in records if predicate(item)]


def canonical_chunks(value: Any) -> Iterator[bytes]:
    """Encode the frozen sorted/compact/UTF-8/LF format without collecting arrays."""
    def encode(item: Any) -> Iterator[bytes]:
        if isinstance(item, Mapping):
            if not all(isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            yield b"{"
            for index, key in enumerate(sorted(item)):
                if index:
                    yield b","
                yield json.dumps(key, ensure_ascii=False).encode("utf-8") + b":"
                yield from encode(item[key])
            yield b"}"
        elif isinstance(item, FileArray):
            yield b"["
            for index, record in enumerate(item):
                if index:
                    yield b","
                yield from encode(record)
            yield b"]"
        else:
            # An ordinary list belongs to one bounded record or small metadata.
            _validate_plain(item)
            yield json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    yield from encode(value)
    yield b"\n"


def _validate_plain(value: Any) -> None:
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        for item in value.values():
            _validate_plain(item)
    elif isinstance(value, list):
        for item in value:
            _validate_plain(item)
    elif value is not None and not isinstance(value, (str, bool, int, float)):
        raise ValueError("unsupported JSON value")


def write_json(path: Path, value: Any, *, replace: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".c6-json-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            for chunk in canonical_chunks(value):
                stream.write(chunk)
            stream.flush()
            os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                os.link(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def content_hash(content: bytes | Path) -> str:
    result = hashlib.sha256()
    if isinstance(content, bytes):
        result.update(content)
    else:
        with content.open("rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                result.update(chunk)
    return result.hexdigest()


def content_size(content: bytes | Path) -> int:
    return len(content) if isinstance(content, bytes) else content.stat().st_size


def copy_stream(source: BinaryIO, target: BinaryIO, *, limit: int = ARCHIVE_LIMIT) -> None:
    count = 0
    while chunk := source.read(min(CHUNK_SIZE, limit - count + 1)):
        count += len(chunk)
        if count > limit:
            raise ValueError("artifact byte limit exceeded")
        target.write(chunk)


def extract_archive(path: Path, destination: Path, *, limit: int = ARCHIVE_LIMIT) -> dict[str, Path]:
    """Extract a small exact file set, rejecting traversal, aliases and ZIP bombs."""
    if path.stat().st_size > limit:
        raise ValueError("compressed artifact byte limit exceeded")
    with zipfile.ZipFile(path) as archive:
        infos = archive.infolist()
        if not 1 <= len(infos) <= 8 or sum(info.file_size for info in infos) > limit:
            raise ValueError("expanded artifact byte or member limit exceeded")
        files: dict[str, Path] = {}
        for info in infos:
            name = info.filename
            mode = info.external_attr >> 16
            if (name in files or not name or name.startswith(".") or "/" in name or "\\" in name
                    or info.orig_filename != name or info.is_dir() or info.flag_bits & 1
                    or (mode & 0o170000) not in {0, 0o100000} or mode & 0o111):
                raise ValueError("unsafe artifact ZIP member")
            files[name] = destination / name
        destination.mkdir(mode=0o700)
        for info in infos:
            with archive.open(info) as source, files[info.filename].open("xb") as target:
                copy_stream(source, target, limit=info.file_size)
            if files[info.filename].stat().st_size != info.file_size:
                raise ValueError("truncated artifact ZIP member")
        return files


class _Reader:
    def __init__(self, stream: TextIO, chunk_size: int, record_limit: int) -> None:
        self.stream, self.chunk_size, self.limit = stream, chunk_size, record_limit
        self.buffer, self.offset, self.eof = "", 0, False

    def more(self) -> None:
        text = self.stream.read(self.chunk_size)
        self.eof = not text
        self.buffer += text

    def consume(self, count: int) -> None:
        self.offset += len(self.buffer[:count].encode("utf-8"))
        self.buffer = self.buffer[count:]

    def peek(self) -> str:
        while True:
            count = len(self.buffer) - len(self.buffer.lstrip(" \r\n\t"))
            self.consume(count)
            if self.buffer or self.eof:
                return self.buffer[:1]
            self.more()

    def expect(self, token: str) -> None:
        if self.peek() != token:
            raise ValueError(f"invalid JSON: expected {token}")
        self.consume(1)

    def value(self) -> tuple[Any, tuple[int, int]]:
        self.peek()
        offset = self.offset
        while True:
            try:
                value, end = _DECODER.raw_decode(self.buffer)
                # A numeric token may straddle chunks (e.g. 1 then 2e3).
                if end < len(self.buffer) and self.buffer[end] in " \r\n\t,:]}":
                    size = len(self.buffer[:end].encode("utf-8"))
                    if size > self.limit:
                        raise ValueError("JSON record limit exceeded")
                    self.consume(end)
                    return value, (offset, size)
                if self.eof:
                    raise ValueError("unterminated or malformed JSON value")
            except json.JSONDecodeError as exc:
                if self.eof:
                    raise ValueError("truncated JSON value") from exc
            if len(self.buffer.encode("utf-8")) > self.limit + self.chunk_size * 4:
                raise ValueError("JSON record limit exceeded")
            self.more()


def load_object(path: Path, *, array_fields: frozenset[str] = frozenset({"evaluations", "completed_items", "results"}),
                chunk_size: int = CHUNK_SIZE, record_limit: int = RECORD_LIMIT) -> dict[str, Any]:
    """Validate all JSON once; index selected root arrays using bounded decoding."""
    if chunk_size < 1 or record_limit < 1:
        raise ValueError("invalid JSON buffer limits")
    result: dict[str, Any] = {}
    with path.open("r", encoding="utf-8", newline="") as stream:
        reader = _Reader(stream, chunk_size, record_limit)
        reader.expect("{")
        if reader.peek() != "}":
            while True:
                key, _ = reader.value()
                if not isinstance(key, str) or key in result:
                    raise ValueError("invalid or duplicate JSON key")
                reader.expect(":")
                if key in array_fields and reader.peek() == "[":
                    reader.expect("[")
                    spans = []
                    if reader.peek() != "]":
                        while True:
                            _, span = reader.value()
                            spans.append(span)
                            if reader.peek() == "]":
                                break
                            reader.expect(",")
                    reader.expect("]")
                    result[key] = FileArray(path, spans)
                else:
                    result[key], _ = reader.value()
                if reader.peek() == "}":
                    break
                reader.expect(",")
        reader.expect("}")
        if reader.peek():
            raise ValueError("trailing JSON data")
    return result
