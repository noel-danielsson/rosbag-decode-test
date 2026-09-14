"""Explicit mappings for current FRB takeoff recordings, in sensor NED frame."""
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from pyproj import Transformer
import rasterio

from rosbag_decode import bag2dfs, list_topics

ROOT = Path(__file__).resolve().parents[3]
NAV = "/x06/sbg/ekf_nav"
EULER = "/x06/sbg/ekf_euler"
DEFAULT_BAG = ROOT / "rosbag2_2025_11_14-14_23_19_foil_i_takeoff"
DEFAULT_RASTER = ROOT / "MARV-Test-Data/rosbag_decode/geo-data/639_31_50_1971.tif"
SCHEMA_VERSION = 1


@dataclass
class Analysis:
    frames: dict
    mappings: dict
    bag: Path
    raster: Path
    definitions: Path
    origin_ns: int
    background: np.ndarray
    bounds: tuple
    source_identity: dict | None = None


def load(bag=DEFAULT_BAG, raster=DEFAULT_RASTER, definitions=ROOT / "FRB-ROS"):
    bag, raster, definitions = map(lambda p: Path(p).resolve(), (bag, raster, definitions))
    available = list_topics(bag).set_index("name")
    for topic in (NAV, EULER):
        if topic not in available.index or available.loc[topic, "count"] == 0:
            raise ValueError(f"Required topic {topic} is absent or empty; use a current FRB takeoff recording")
    raw = bag2dfs(bag, [NAV, EULER], definitions_root=definitions)
    frames, mappings = {}, {}
    for topic, df in raw.items():
        headers = df[f"{topic}/header"]
        if not headers.map(lambda h: h["frame_id"] == "imu_link_ned").all():
            raise ValueError(f"{topic}: expected recorded frame imu_link_ned; cannot assume NED")
        out = pd.DataFrame(index=df.index)
        out["source_row"] = np.arange(len(df), dtype=np.int64)
        out["timestamp_ns"] = df.index.as_unit("ns").asi8
        out["timestamp_utc"] = [stamp.isoformat() for stamp in df.index]
        out["header_timestamp_ns"] = headers.map(
            lambda h: h["stamp"]["sec"] * 1_000_000_000 + h["stamp"]["nanosec"])
        out["sensor_timestamp_us"] = df[f"{topic}/time_stamp"].to_numpy()
        spec = {}

        def field(name, path, unit):
            values = df[f"{topic}/{path[0]}"]
            for key in path[1:]:
                values = values.map(lambda value, key=key: value[key])
            out[name] = values.to_numpy()
            spec[name] = {"source": ".".join(path), "unit": unit}

        if topic == NAV:
            for name, unit in (("latitude", "deg"), ("longitude", "deg"), ("altitude", "m")):
                field(name, [name], unit)
            for vector, unit in (("velocity", "m/s"), ("velocity_accuracy", "m/s"), ("position_accuracy", "m")):
                for axis, direction in zip("xyz", ("north", "east", "down")):
                    field(f"{vector}_{direction}", [vector, axis], unit)
            out["horizontal_speed"] = np.hypot(out.velocity_north, out.velocity_east)
            spec["horizontal_speed"] = {"expression": "hypot(velocity.x, velocity.y)", "unit": "m/s"}
        else:
            for axis, name in zip("xyz", ("roll", "pitch", "yaw")):
                field(name + "_rad", ["angle", axis], "rad")
                field(name + "_accuracy_rad", ["accuracy", axis], "rad")
                out[name + "_deg"] = np.rad2deg(out[name + "_rad"])
                spec[name + "_deg"] = {"expression": f"rad2deg({name}_rad)", "unit": "deg"}
        for name in df[f"{topic}/status"].iloc[0]:
            field(name, ["status", name], "code" if name == "solution_mode" else "bool")
        frames[topic], mappings[topic] = out, spec
    with rasterio.open(raster) as src:
        if src.crs is None or src.crs.to_epsg() != 3006:
            raise ValueError("Background must use SWEREF 99 TM (EPSG:3006)")
        background = src.read(1, out_shape=(1500, 1500), masked=True).filled(0)
        bounds = tuple(src.bounds)
        nav = frames[NAV]
        nav["easting"], nav["northing"] = Transformer.from_crs(
            "EPSG:4326", src.crs, always_xy=True).transform(nav.longitude.to_numpy(), nav.latitude.to_numpy())
        for name in ("easting", "northing"):
            mappings[NAV][name] = {"expression": "EPSG:4326 → EPSG:3006, longitude first, always_xy=True", "unit": "m"}
    data = Analysis(frames, mappings, bag, raster, definitions,
                    min(int(d.index.as_unit("ns").asi8.min()) for d in frames.values()), background, bounds)
    from .export import identity
    data.source_identity = identity(data)
    return data
