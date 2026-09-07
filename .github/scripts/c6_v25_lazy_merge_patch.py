from pathlib import Path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"missing patch marker: {label}")
    return text.replace(old, new, 1)


stream = Path("quantfusion/io/c6_stream.py")
text = stream.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from contextlib import contextmanager\n",
    "from contextlib import ExitStack, contextmanager\n",
    "ExitStack import",
)
marker = '''
def select_records(records: Sequence[Any], predicate: Callable[[Any], bool]) -> Sequence[Any]:
    return records.select(predicate) if isinstance(records, FileArray) else [item for item in records if predicate(item)]
'''
replacement = '''
class MultiFileArray(Sequence[Any]):
    """Read-only ordered view over record spans stored in multiple immutable files."""

    def __init__(
        self,
        refs: Sequence[tuple[Path, tuple[int, int]]],
        *,
        field: str | None = None,
    ) -> None:
        self.refs, self.field = list(refs), field

    def __len__(self) -> int:
        return len(self.refs)

    @overload
    def __getitem__(self, key: int) -> Any: ...

    @overload
    def __getitem__(self, key: slice) -> MultiFileArray: ...

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            return MultiFileArray(self.refs[key], field=self.field)
        path, span = self.refs[key]
        with open_json_bytes(path) as stream:
            return self._read(stream, span)

    def _read(self, stream: BinaryIO | gzip.GzipFile, span: tuple[int, int]) -> Any:
        offset, size = span
        stream.seek(offset)
        raw = stream.read(size)
        if len(raw) != size:
            raise ValueError("indexed JSON record was truncated")
        value = _DECODER.decode(raw.decode("utf-8"))
        return value if self.field is None else value[self.field]

    def __iter__(self) -> Iterator[Any]:
        with ExitStack() as stack:
            streams: dict[Path, BinaryIO | gzip.GzipFile] = {}
            for path, span in self.refs:
                stream = streams.get(path)
                if stream is None:
                    stream = stack.enter_context(open_json_bytes(path))
                    streams[path] = stream
                yield self._read(stream, span)

    def select(self, predicate: Callable[[Any], bool]) -> MultiFileArray:
        refs = [ref for ref, item in zip(self.refs, self) if predicate(item)]
        return MultiFileArray(refs, field=self.field)

    def project(self, field: str) -> MultiFileArray:
        if self.field is not None:
            raise ValueError("record view already projected")
        return MultiFileArray(self.refs, field=field)


class ChainedArray(Sequence[Any]):
    """Small composition wrapper that preserves file-backed parts without copying."""

    def __init__(self, parts: Sequence[Sequence[Any]]) -> None:
        self.parts = [part for part in parts if len(part)]

    def __len__(self) -> int:
        return sum(len(part) for part in self.parts)

    @overload
    def __getitem__(self, key: int) -> Any: ...

    @overload
    def __getitem__(self, key: slice) -> list[Any]: ...

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            return [self[index] for index in range(*key.indices(len(self)))]
        index = key if key >= 0 else len(self) + key
        if index < 0:
            raise IndexError(key)
        for part in self.parts:
            if index < len(part):
                return part[index]
            index -= len(part)
        raise IndexError(key)

    def __iter__(self) -> Iterator[Any]:
        for part in self.parts:
            yield from part

    def select(self, predicate: Callable[[Any], bool]) -> ChainedArray:
        selected: list[Sequence[Any]] = []
        for part in self.parts:
            if isinstance(part, (FileArray, MultiFileArray, ChainedArray)):
                selected.append(part.select(predicate))
            else:
                selected.append([item for item in part if predicate(item)])
        return ChainedArray(selected)


def select_records(records: Sequence[Any], predicate: Callable[[Any], bool]) -> Sequence[Any]:
    if isinstance(records, (FileArray, MultiFileArray, ChainedArray)):
        return records.select(predicate)
    return [item for item in records if predicate(item)]
'''
text = replace_once(text, marker, replacement, "streaming sequence classes")
text = replace_once(
    text,
    "        elif isinstance(item, FileArray):\n",
    "        elif isinstance(item, (FileArray, MultiFileArray, ChainedArray)):\n",
    "canonical streaming arrays",
)
stream.write_text(text, encoding="utf-8")

parallel = Path("quantfusion/application/c6_parallel_l1.py")
text = parallel.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from quantfusion.io.c6_stream import FileArray, load_object\n",
    "from quantfusion.io.c6_stream import FileArray, MultiFileArray, load_object\n",
    "MultiFileArray import",
)
text = replace_once(
    text,
    '''    by_id: dict[str, dict[str, Any]] = {}
    seen_shards: set[int] = set()
''',
    '''    by_id: dict[str, dict[str, Any]] = {}
    by_ref: dict[str, tuple[Path, tuple[int, int]]] = {}
    seen_ids: set[str] = set()
    seen_shards: set[int] = set()
''',
    "lightweight identity maps",
)
old_loop = '''        observed_shard: list[str] = []
        for item in records:
            item_id, result = _validate_record(
            item,
            seen_ids=set(by_id),
            verify_result_hash=not attestations_required,
        )
            observed_shard.append(item_id)
            if prereg is not None and not attestations_required:
                validate_checkpoint_item(item, prereg)
            by_id[item_id] = result
        if observed_shard != expected_shard:
            raise ValueError("parallel L1 shard does not contain its exact partition")
        seen_shards.add(index)
    if seen_shards != set(range(shard_count)) or set(by_id) != set(ids):
        raise ValueError("parallel L1 shard union is incomplete")
    return [by_id[item] for item in ids]
'''
new_loop = '''        observed_shard: list[str] = []
        if attestations_required and not isinstance(records, FileArray):
            raise ValueError("attested parallel L1 requires indexed shard records")
        spans = records.spans if isinstance(records, FileArray) else [None] * len(records)
        for span, item in zip(spans, records):
            item_id, result = _validate_record(
                item,
                seen_ids=seen_ids,
                verify_result_hash=not attestations_required,
            )
            observed_shard.append(item_id)
            seen_ids.add(item_id)
            if prereg is not None and not attestations_required:
                validate_checkpoint_item(item, prereg)
            if attestations_required:
                assert isinstance(span, tuple)
                by_ref[item_id] = (records.path, span)
            else:
                by_id[item_id] = result
        if observed_shard != expected_shard:
            raise ValueError("parallel L1 shard does not contain its exact partition")
        seen_shards.add(index)
    if seen_shards != set(range(shard_count)) or seen_ids != set(ids):
        raise ValueError("parallel L1 shard union is incomplete")
    if attestations_required:
        return MultiFileArray([by_ref[item] for item in ids]).project("result")
    return [by_id[item] for item in ids]
'''
text = replace_once(text, old_loop, new_loop, "disk-backed attested merge")
text = replace_once(
    text,
    ") -> list[dict[str, Any]]:\n    from quantfusion.application.c6_bound_run import validate_checkpoint_item\n",
    ") -> Sequence[dict[str, Any]]:\n    from quantfusion.application.c6_bound_run import validate_checkpoint_item\n",
    "merge return type",
)
text = replace_once(
    text,
    ") -> list[dict[str, Any]]:\n    ids, _ = core_l1_tasks(prereg, binding)\n",
    ") -> Sequence[dict[str, Any]]:\n    ids, _ = core_l1_tasks(prereg, binding)\n",
    "loader return type",
)
parallel.write_text(text, encoding="utf-8")

diagnostics = Path("quantfusion/application/c6_diagnostics.py")
text = diagnostics.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from quantfusion.io.c6_stream import load_object, producer_payload_path, select_records, write_json\n",
    "from quantfusion.io.c6_stream import ChainedArray, load_object, producer_payload_path, select_records, write_json\n",
    "ChainedArray import",
)
text = replace_once(
    text,
    "            evaluations = [*evaluations, *intervention_results]\n",
    "            evaluations = ChainedArray((evaluations, intervention_results))\n",
    "bounded intervention append",
)
diagnostics.write_text(text, encoding="utf-8")

test = Path("tests/c6_non_economic/test_c6_parallel_l1_attestation.py")
text = test.read_text(encoding="utf-8")
text = replace_once(
    text,
    "from quantfusion.io.c6_stream import load_object, write_json\n",
    "from quantfusion.io.c6_stream import (\n    ChainedArray,\n    MultiFileArray,\n    load_object,\n    select_records,\n    write_json,\n)\n",
    "streaming test imports",
)
text = replace_once(
    text,
    '    assert results == [{"value": index} for index in range(len(ids))]\n\n    payload = load_object(path)\n',
    '''    assert isinstance(results, MultiFileArray)
    assert list(results) == [{"value": index} for index in range(len(ids))]
    selected = select_records(results, lambda item: item["value"] % 2 == 0)
    assert isinstance(selected, MultiFileArray)
    assert list(selected) == [
        {"value": index} for index in range(0, len(ids), 2)
    ]
    chained = ChainedArray((results, [{"value": 99}]))
    assert len(chained) == len(ids) + 1
    assert chained[-1] == {"value": 99}
    output = tmp_path / "streamed.json.gz"
    write_json(output, {"evaluations": chained})
    assert list(load_object(output)["evaluations"]) == [
        *[{"value": index} for index in range(len(ids))],
        {"value": 99},
    ]

    payload = load_object(path)
''',
    "lazy merge regression",
)
test.write_text(text, encoding="utf-8")
