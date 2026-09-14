"""CDR decoding using a Galactic typestore and FRB message definitions."""

from collections.abc import Iterable
from dataclasses import fields, is_dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

import pandas as pd
from rosbags.rosbag2 import Reader
from rosbags.typesys import Stores, get_types_from_msg, get_typestore


class DecodeError(RuntimeError):
    """A message could not be decoded; the error identifies its bag location."""


def _typestore(definitions_root=None):
    root = (Path(definitions_root) if definitions_root is not None
            else Path(__file__).resolve().parents[2] / "FRB-ROS")
    if not root.is_dir():
        raise FileNotFoundError(
            f"Message definitions not found at {root}. Initialize FRB-ROS or "
            "pass definitions_root pointing to its checkout."
        )
    definitions = {}
    for manifest in sorted(root.rglob("package.xml")):
        messages = sorted((manifest.parent / "msg").glob("*.msg"))
        if not messages:
            continue
        package = ET.parse(manifest).findtext("name")
        if not package or not package.strip():
            raise ValueError(f"Missing package name in {manifest}")
        for message in messages:
            name = f"{package.strip()}/msg/{message.stem}"
            definitions.update(get_types_from_msg(message.read_text(encoding="utf-8"), name))
    if not definitions:
        raise FileNotFoundError(f"No ROS message definitions found under {root}")
    store = get_typestore(Stores.ROS2_GALACTIC)
    store.register(definitions)
    return store


def list_topics(bag_path) -> pd.DataFrame:
    """Return columns name, msgtype, count, including zero-count topics."""
    with Reader(Path(bag_path)) as reader:
        return pd.DataFrame(
            [(name, info.msgtype, info.msgcount) for name, info in reader.topics.items()],
            columns=["name", "msgtype", "count"],
        )


def _value(value):
    if is_dataclass(value):
        return {field.name: _value(getattr(value, field.name))
                for field in fields(value) if not field.name.startswith("__")}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value  # Numeric numpy arrays remain array-valued cells.


def _load(bag_path, topics, definitions_root, ignored=(), prefix=None):
    with Reader(Path(bag_path)) as reader:
        selected = ([name for name, info in reader.topics.items() if info.msgcount]
                    if topics is None else list(dict.fromkeys(topics)))
        unknown = set(selected) - reader.topics.keys()
        if unknown:
            raise ValueError(f"Unknown topic(s): {', '.join(sorted(unknown))}")
        if not selected:
            return {}
        store = _typestore(definitions_root)
        keys = {}
        for name in selected:
            msgtype = reader.topics[name].msgtype
            if msgtype not in store.fielddefs:
                raise ValueError(f"Missing definition for topic {name!r}, type {msgtype!r}")
            keys[name] = [key for key, _ in store.fielddefs[msgtype][1] if key not in ignored]
        rows = {name: [] for name in selected}
        timestamps = {name: [] for name in selected}
        connections = [connection for connection in reader.connections if connection.topic in rows]
        for connection, timestamp, raw in reader.messages(connections=connections):
            name = connection.topic
            try:
                message = store.deserialize_cdr(raw, connection.msgtype)
                row = [_value(getattr(message, key)) for key in keys[name]]
            except Exception as exc:
                raise DecodeError(
                    f"Failed to decode topic {name!r}, type {connection.msgtype!r}, "
                    f"timestamp {timestamp}: {exc}"
                ) from exc
            rows[name].append(row)
            timestamps[name].append(timestamp)
        return {
            name: pd.DataFrame(
                rows[name],
                columns=[(name + "/" if prefix is None else prefix) + key for key in keys[name]],
                index=pd.DatetimeIndex(pd.to_datetime(timestamps[name], unit="ns", utc=True),
                                       name="timestamp").as_unit("ns"),
            ) for name in selected
        }


def topic2df(bag_path, topic_name, key_ignore_list=None, prefix=None, *, definitions_root=None):
    """Load one topic; ignored fields are top-level names, prefix is concatenated verbatim."""
    return _load(bag_path, [topic_name], definitions_root,
                 ignored=() if key_ignore_list is None else key_ignore_list,
                 prefix=prefix)[topic_name]


def bag2dfs(bag_path, topics: Iterable[str] | None = None, *, definitions_root=None):
    """Read selected topics once; by default load every populated topic."""
    if isinstance(topics, str):
        topics = [topics]
    return _load(bag_path, topics, definitions_root)
