"""Focused native-measurement and interaction checks with tiny generated sources."""
import json
from html.parser import HTMLParser
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.request import urlopen
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("panel")
rasterio = pytest.importorskip("rasterio")
from rasterio.transform import from_origin
from rosbags.rosbag2 import Writer

from rosbag_decode.decoder import _typestore
from rosbag_decode.analysis.app import create_app
from rosbag_decode.analysis.export import export_selection, replay
from rosbag_decode.analysis.selection import from_drag, mask, parse_utc, utc
from rosbag_decode.analysis.takeoff import NAV, EULER, ROOT, load
from bokeh.events import SelectionGeometry

STAMP = 1_700_000_000_123_456_789


@pytest.fixture
def sources(tmp_path):
    bag, raster = tmp_path / "bag", tmp_path / "background_1971.tif"
    store = _typestore()
    types = store.types
    vector = types["geometry_msgs/msg/Vector3"]
    status_type = types["sbg_driver/msg/SbgEkfStatus"]
    status = status_type(**{name: 4 if name == "solution_mode" else True
                            for name, _ in store.fielddefs["sbg_driver/msg/SbgEkfStatus"][1]})
    header = types["std_msgs/msg/Header"](types["builtin_interfaces/msg/Time"](12, 34), "imu_link_ned")
    stamps = [STAMP, STAMP+1, STAMP+1, STAMP+2_000_000_000, STAMP+10_000_000_000, STAMP+10_000_000_001]
    with Writer(bag, version=9) as writer:
        nav = writer.add_connection(NAV, "sbg_driver/msg/SbgEkfNav", typestore=store)
        euler = writer.add_connection(EULER, "sbg_driver/msg/SbgEkfEuler", typestore=store)
        for i, stamp in enumerate(stamps):
            status.heading_valid = i != 3
            message = types[nav.msgtype](header, i, vector(3., 4., 0.), vector(.1, .1, .1),
                                        57.672 + i*0.000001, 11.8437, float("nan") if i == 3 else 1.,
                                        0., vector(.5, .5, .5), status)
            writer.write(nav, stamp, store.serialize_cdr(message, nav.msgtype))
            message = types[euler.msgtype](header, i, vector(.1, .2, 3.13 if i < 3 else -3.13),
                                          vector(.01, .01, .01), status)
            writer.write(euler, stamp+2, store.serialize_cdr(message, euler.msgtype))
    with rasterio.open(raster, "w", driver="GTiff", width=20, height=20, count=1,
                       dtype="uint8", crs="EPSG:3006", transform=from_origin(310000, 6400000, 250, 250)) as src:
        src.write(np.full((1, 20, 20), 120, dtype=np.uint8))
    return bag, raster


@pytest.fixture
def data(sources):
    return load(*sources)


def test_mappings_native_timing_and_angles(data):
    nav, euler = data.frames[NAV], data.frames[EULER]
    assert len(nav) == len(euler) == 6
    assert nav.timestamp_ns.iloc[0] == STAMP
    assert nav.index.duplicated().sum() == 1
    assert euler.timestamp_ns.iloc[0] == STAMP + 2
    assert nav.header_timestamp_ns.iloc[0] == 12_000_000_034
    assert nav.horizontal_speed.tolist() == [5.] * 6
    assert np.isnan(nav.altitude.iloc[3])
    assert not nav.heading_valid.iloc[3]
    np.testing.assert_allclose(euler.yaw_deg, np.rad2deg([3.13]*3 + [-3.13]*3))
    assert euler.yaw_rad.iloc[3] < 0  # no unwrap/interpolation across the boundary
    assert nav.easting.between(311000, 312000).all()
    assert nav.northing.between(6396000, 6397000).all()


def test_intervals_precision_and_membership(data):
    assert parse_utc(utc(STAMP)) == STAMP
    assert from_drag(0.000000001, 0, STAMP) == (STAMP, STAMP+1)
    with pytest.raises(ValueError, match="timezone"):
        parse_utc("2025-01-01")
    ranges = [(STAMP, STAMP+1), (STAMP+10_000_000_000, STAMP+10_000_000_001)]
    selected = data.frames[NAV].loc[mask(data.frames[NAV], ranges)]
    assert selected.source_row.tolist() == [0, 1, 2, 4, 5]
    assert mask(data.frames[NAV], ranges + [ranges[0]]).sum() == 5
    assert not mask(data.frames[NAV], []).any()


def test_export_and_replay(data, tmp_path):
    intervals = [(STAMP, STAMP+1), (STAMP+10_000_000_000, STAMP+10_000_000_001)]
    destination = export_selection(data, intervals, tmp_path / "exports", interval_names=["Acceleration", "Turn"])
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["intervals"][0]["start_ns"] == str(STAMP)
    assert [item["name"] for item in manifest["intervals"]] == ["Acceleration", "Turn"]
    frame = pd.read_csv(destination / "ekf_nav.csv", dtype={"timestamp_ns": "int64"})
    assert frame.source_row.tolist() == [0, 1, 2, 4, 5]
    assert frame.timestamp_ns.iloc[0] == STAMP
    assert manifest["outputs"][EULER]["rows"] == 0  # native offsets, no alignment
    repeated = replay(destination / "manifest.json", tmp_path / "exports")
    assert json.loads((repeated / "manifest.json").read_text(encoding="utf-8"))["intervals"] == manifest["intervals"]
    assert (repeated / "ekf_nav.csv").read_bytes() == (destination / "ekf_nav.csv").read_bytes()
    with pytest.raises(ValueError, match="interval"):
        export_selection(data, [], tmp_path)
    with pytest.raises(ValueError, match="no measurements"):
        export_selection(data, [(0, 1)], tmp_path)
    manifest["identity"]["raster"] = "changed"
    (destination / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        replay(destination / "manifest.json", tmp_path)


def test_app_selection_independent_of_display(data, tmp_path):
    app = create_app(data, tmp_path)
    assert app.export.disabled
    app.drag(SelectionGeometry(model=None, geometry={"type": "rect", "x0": 0, "x1": .000000001}, final=True))
    assert not app.intervals and app.export.disabled
    app.interval_name.value = "Acceleration"
    app.add_interval()
    assert app.interval_names == ["Acceleration"]
    assert app.saved.value == 0
    app.set_preview((STAMP+10_000_000_000, STAMP+10_000_000_001))
    app.add_interval()
    assert len(app.intervals) == 2 and not app.export.disabled
    assert app.interval_names == ["Acceleration", "Interval 2"]
    app.view.get_root()  # Validate complete Bokeh document construction.
    # Display data changes cannot alter selection or export source membership.
    app.plot_layers[0][3].data = dict(x=[], y=[], time=[])
    output = app.export_current()
    assert pd.read_csv(output / "ekf_nav.csv").source_row.tolist() == [0, 1, 2, 4, 5]
    from zipfile import ZipFile
    with ZipFile(app.download_current()) as archive:
        assert set(archive.namelist()) == {"ekf_nav.csv", "ekf_euler.csv", "manifest.json"}
        assert archive.read("ekf_nav.csv") == (output / "ekf_nav.csv").read_bytes()
        assert json.loads(archive.read("manifest.json"))["intervals"][0]["name"] == "Acceleration"
    assert "browser" in app.export_status.object
    app.saved.value = 0
    app.saved_name.value = "Initial acceleration"
    app.refresh()  # Plot refreshes must not overwrite a name being edited.
    assert app.saved_name.value == "Initial acceleration"
    app.rename_interval()
    assert app.interval_names[0] == "Initial acceleration"
    app.start.value, app.end.value = utc(STAMP), utc(STAMP)
    app.update_interval()
    assert app.intervals[0] == (STAMP, STAMP)
    app.remove_interval()
    assert len(app.intervals) == 1
    assert app.interval_names == ["Interval 2"]
    app.clear_selection()
    assert not app.intervals and app.preview is None and app.export.disabled
    assert not app.interval_names
    assert len(app.table_frame) == 0
    with pytest.raises(ValueError, match="Export failed"):
        app.download_current()
    assert "Export failed" in app.export_status.object


def test_hover_is_bounded_and_compact(data, tmp_path):
    from bokeh.models import HoverTool
    app = create_app(data, tmp_path)
    for plot in [app.map, *app.time_plots]:
        hovers = plot.select(type=HoverTool)
        assert hovers
        for hover in hovers:
            assert hover.limit == 1
            assert hover.mode == "mouse"
            assert len(hover.tooltips) <= 3
            assert all("@utc" not in value and "@row" not in value for _, value in hover.tooltips)


def test_missing_empty_and_frame(sources, tmp_path, monkeypatch):
    bag, raster = sources
    empty = tmp_path / "empty"
    store = _typestore()
    with Writer(empty, version=9) as writer:
        writer.add_connection(NAV, "sbg_driver/msg/SbgEkfNav", typestore=store)
    with pytest.raises(ValueError, match="absent or empty"):
        load(empty, raster)
    import rosbag_decode.analysis.takeoff as module
    original = module.bag2dfs
    def wrong_frame(*args, **kwargs):
        frames = original(*args, **kwargs)
        frames[NAV].iloc[0][f"{NAV}/header"]["frame_id"] = "unknown"
        return frames
    monkeypatch.setattr(module, "bag2dfs", wrong_frame)
    with pytest.raises(ValueError, match="imu_link_ned"):
        load(bag, raster)


def test_fresh_process_server(sources, tmp_path):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    command = [sys.executable, "-m", "rosbag_decode.analysis.cli", "--bag", str(sources[0]),
               "--raster", str(sources[1]), "--output-dir", str(tmp_path / "exports"), "--port", str(port)]
    with (tmp_path / "server.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(command, cwd=tmp_path, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    break
                try:
                    with urlopen(f"http://127.0.0.1:{port}", timeout=2) as response:
                        assert response.status == 200
                        html = response.read().decode("utf-8")
                        assert "Bokeh" in html
                        class Scripts(HTMLParser):
                            def __init__(self):
                                super().__init__()
                                self.urls = []
                            def handle_starttag(self, tag, attrs):
                                if tag == "script" and "src" in dict(attrs):
                                    self.urls.append(dict(attrs)["src"])
                        scripts = Scripts()
                        scripts.feed(html)
                        assert any("panel.min.js" in url for url in scripts.urls)
                        for url in scripts.urls:
                            with urlopen(urljoin(f"http://127.0.0.1:{port}/", url), timeout=10) as asset:
                                assert asset.status == 200
                                assert len(asset.read()) > 0
                        # Complete a real Bokeh protocol handshake and deserialize the document.
                        import panel as pn
                        pn.extension()
                        from bokeh.client import pull_session
                        session = pull_session(url=f"http://127.0.0.1:{port}")
                        assert len(session.document.roots) > 0
                        session.close()
                        return
                except OSError:
                    time.sleep(.2)
            log.seek(0)
            pytest.fail(log.read())
        finally:
            process.terminate()
            process.wait(timeout=10)


def test_source_change_refuses_export(data, tmp_path):
    (data.bag / "added.txt").write_text("source changed", encoding="utf-8")
    with pytest.raises(ValueError, match="changed since loading"):
        export_selection(data, [(STAMP, STAMP+1)], tmp_path / "export")
    assert not (tmp_path / "export").exists()


@pytest.mark.integration
def test_real_takeoff_analysis(tmp_path):
    from rosbag_decode.analysis.takeoff import DEFAULT_BAG, DEFAULT_RASTER
    if not DEFAULT_BAG.exists() or not DEFAULT_RASTER.exists():
        pytest.skip("Local takeoff bag and historical raster required")
    data = load()
    assert {topic: len(frame) for topic, frame in data.frames.items()} == {NAV: 13828, EULER: 13826}
    start = data.origin_ns
    intervals = [(start+10_000_000_000, start+20_000_000_000),
                 (start+40_000_000_000, start+50_000_000_000)]
    destination = export_selection(data, intervals, tmp_path)
    for topic, source in data.frames.items():
        stamps = source.timestamp_ns
        expected = source.loc[((stamps >= intervals[0][0]) & (stamps <= intervals[0][1])) |
                              ((stamps >= intervals[1][0]) & (stamps <= intervals[1][1]))]
        exported = pd.read_csv(destination / (topic.rsplit("/", 1)[-1] + ".csv"), float_precision="round_trip")
        assert exported.source_row.tolist() == expected.source_row.tolist()
        assert exported.timestamp_ns.tolist() == expected.timestamp_ns.tolist()
    reproduced = replay(destination / "manifest.json", tmp_path)
    assert (destination / "ekf_nav.csv").read_bytes() == (reproduced / "ekf_nav.csv").read_bytes()
