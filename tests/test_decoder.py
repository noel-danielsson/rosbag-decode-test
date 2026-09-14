from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from rosbags.rosbag2 import Writer
from rosbags.typesys import get_types_from_msg

from rosbag_decode import DecodeError, bag2dfs, list_topics, topic2df
from rosbag_decode.decoder import _typestore

ROOT = Path(__file__).resolve().parents[1]
BAG = ROOT / "rosbag2_2025_11_14-14_23_19_foil_i_takeoff"
STAMP = 1_700_000_000_123_456_789


@pytest.fixture
def bag(tmp_path):
    store = _typestore()
    types = store.types
    path = tmp_path / "bag"
    with Writer(path, version=9) as writer:
        scalar = writer.add_connection("/scalar", "std_msgs/msg/UInt64", typestore=store)
        nested = writer.add_connection("/nested", "geometry_msgs/msg/PoseStamped", typestore=store)
        array = writer.add_connection("/array", "std_msgs/msg/UInt8MultiArray", typestore=store)
        custom = writer.add_connection("/custom", "frb_msgs/msg/SBGPose", typestore=store)
        writer.add_connection("/empty", "std_msgs/msg/Bool", typestore=store)
        for stamp, value in [(STAMP, 2**64 - 1), (STAMP, 2**63 + 1), (STAMP + 1, 0)]:
            writer.write(scalar, stamp, store.serialize_cdr(types[scalar.msgtype](value), scalar.msgtype))
        message = types[nested.msgtype](
            types["std_msgs/msg/Header"](types["builtin_interfaces/msg/Time"](12, 34), "map"),
            types["geometry_msgs/msg/Pose"](types["geometry_msgs/msg/Point"](1., 2., 3.),
                                            types["geometry_msgs/msg/Quaternion"](0., 0., 0., 1.)))
        writer.write(nested, STAMP, store.serialize_cdr(message, nested.msgtype))
        message = types[array.msgtype](types["std_msgs/msg/MultiArrayLayout"]([], 0),
                                       np.array([0, 255], dtype=np.uint8))
        writer.write(array, STAMP, store.serialize_cdr(message, array.msgtype))
        vector = types["geometry_msgs/msg/Vector3"](1., 2., 3.)
        message = types[custom.msgtype](vector, vector, vector, vector, vector, 5., vector)
        writer.write(custom, STAMP, store.serialize_cdr(message, custom.msgtype))
    return path


def test_values_timestamps_and_selection(bag):
    frames = bag2dfs(bag)
    assert set(frames) == {"/scalar", "/nested", "/array", "/custom"}
    df = frames["/scalar"]
    assert df.index.name == "timestamp"
    assert str(df.index.tz) == "UTC"
    assert df.index.asi8.tolist() == [STAMP, STAMP, STAMP + 1]
    assert df.iloc[:, 0].tolist() == [2**64 - 1, 2**63 + 1, 0]
    assert frames["/nested"].iloc[0]["/nested/header"] == {
        "stamp": {"sec": 12, "nanosec": 34}, "frame_id": "map"}
    assert frames["/nested"].iloc[0]["/nested/pose"]["position"]["x"] == 1.
    np.testing.assert_array_equal(frames["/array"].iloc[0]["/array/data"], [0, 255])
    assert frames["/custom"].iloc[0]["/custom/pos_geo"] == {"x": 1., "y": 2., "z": 3.}
    pd.testing.assert_frame_equal(bag2dfs(bag, ["/scalar"])["/scalar"], df)
    assert bag2dfs(bag, []) == {}


def test_prefix_ignore_and_empty(bag):
    assert topic2df(bag, "/nested", ["header", "absent"], "p.").columns.tolist() == ["p.pose"]
    assert topic2df(bag, "/scalar", prefix="").columns.tolist() == ["data"]
    assert topic2df(bag, "/scalar", ["data"]).shape == (3, 0)
    empty = topic2df(bag, "/empty")
    assert empty.shape == (0, 1)
    assert empty.columns.tolist() == ["/empty/data"]
    assert str(empty.index.dtype) == "datetime64[ns, UTC]"
    assert list_topics(bag).set_index("name").loc["/empty", "count"] == 0


def test_errors(bag, tmp_path):
    with pytest.raises(ValueError, match="Unknown topic.*missing"):
        topic2df(bag, "/missing")
    with pytest.raises(FileNotFoundError, match="definitions"):
        topic2df(bag, "/scalar", definitions_root=tmp_path / "missing")
    with pytest.raises(FileNotFoundError, match="No ROS message definitions"):
        topic2df(bag, "/scalar", definitions_root=tmp_path)


def test_decode_failure_and_missing_type(tmp_path):
    store = _typestore()
    store.register(get_types_from_msg("int32 value", "unknown/msg/Test"))
    path = tmp_path / "bad"
    with Writer(path, version=9) as writer:
        connection = writer.add_connection("/bad", "std_msgs/msg/UInt64", typestore=store)
        writer.write(connection, STAMP, b"bad")
        writer.add_connection("/unknown", "unknown/msg/Test", typestore=store)
    with pytest.raises(DecodeError, match=f"/bad.*std_msgs/msg/UInt64.*{STAMP}"):
        topic2df(path, "/bad")
    with pytest.raises(ValueError, match="Missing definition.*unknown/msg/Test"):
        topic2df(path, "/unknown")


def test_all_custom_definitions():
    store = _typestore(ROOT / "FRB-ROS")
    assert len([name for name in store.types if name.startswith(("frb_msgs/", "sbg_driver/"))]) == 48


@pytest.mark.integration
def test_complete_takeoff():
    if not BAG.exists():
        pytest.skip("Bundled takeoff bag not available")
    topics = list_topics(BAG).set_index("name")
    frames = bag2dfs(BAG)
    assert len(frames) == 35
    assert sum(map(len, frames.values())) == 165_935
    assert {name: len(df) for name, df in frames.items()} == topics.loc[topics["count"] > 0, "count"].to_dict()
