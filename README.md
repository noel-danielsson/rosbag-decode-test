# Rosbag to pandas

Decode ROS 2 bags without installing ROS. Uses Python 3.13.5, rosbags 0.11.5,
pandas 3.0.5, and all 48 FRB-ROS custom message definitions with a Galactic typestore.
Package names come from `package.xml`, so `sbg_ros2` is registered as `sbg_driver`.

## Setup (Windows PowerShell)

Install uv, then run from this repository:

```powershell
git submodule update --init --recursive
uv sync --locked
uv run --locked python examples/takeoff.py
uv run --locked pytest -q
```

Keep `uv.lock` in version control. `--locked` rejects dependency changes that would
modify the lockfile; see [uv's reproducibility workflow](https://docs.astral.sh/uv/concepts/projects/sync/).
The submodules must stay at their pinned revisions (do not use `--remote`):

- FRB-ROS: `94ce66c4b365898c169bfa66d7c888d5b01db0db`
- MARV-Test-Data: `01caaeaf4f7425e30646417b7c59d2e5653ec898`

The takeoff directory `rosbag2_2025_11_14-14_23_19_foil_i_takeoff` must contain
its `metadata.yaml` and `.db3` file. It is a local dataset, not a package dependency.

## Interface

```python
from rosbag_decode import list_topics, topic2df, bag2dfs

bag = "rosbag2_2025_11_14-14_23_19_foil_i_takeoff"
topics = list_topics(bag)  # DataFrame: name, msgtype, count; includes empty topics
pose = topic2df(bag, "/x06/frb/nav/sbg_pose")
latitude = pose["/x06/frb/nav/sbg_pose/pos_geo"].map(lambda value: value["x"])
frames = bag2dfs(bag)  # all populated topics, read in one pass
selected = bag2dfs(bag, ["/x06/frb/nav/sbg_pose", "/x06/sbg/imu_data"])
```

Each row is one message. The `timestamp` index uses integer bag timestamps as UTC
nanoseconds, retaining duplicates. Nested messages are recursive dictionary cells;
numeric arrays remain NumPy array cells, and message sequences remain lists of
dictionaries. ROS implementation metadata and constants are excluded.

`topic2df(bag_path, topic_name, key_ignore_list=None, prefix=None, *, definitions_root=None)`
accepts unprefixed top-level field names to ignore. The default column prefix is
`<topic>/`; a supplied prefix is concatenated verbatim (`"pose."` gives `pose.pos_geo`,
`""` gives `pos_geo`). Existing empty topics return their schema with a UTC empty
index. Unknown topics raise `ValueError`; missing definitions raise `FileNotFoundError`
or `ValueError`; message decoding errors include topic, type, and integer timestamp.

Both loading functions accept `definitions_root=Path(".../FRB-ROS")` for an alternative
checkout. The default resolves relative to the source project, independently of the
working directory. For a wheel installation outside the checkout, pass this argument.
Registration uses the supported [rosbags typestore API](https://ternaris.gitlab.io/rosbags/topics/typesys.html).

The full integration test checks **165,935 rows across 35 populated topics** and every
per-topic count against metadata. It skips if the local bag is absent; run only that
check with `uv run --locked pytest -q -m integration`. Synthetic-bag tests cover
standard/custom messages, nested values, arrays, prefixes, ignored fields, empty
topics, errors, duplicate timestamps, and nanosecond precision.

Loading is in memory. Keep merging, resampling, plotting, and exports in subsequent
analysis scripts; generated files can go in the ignored `outputs/` directory.

## GPS track example

```powershell
uv sync --locked --group analysis
uv run --locked --group analysis python examples/plot_takeoff_gps.py
```

Saves `outputs/takeoff_gps_track.png`: the takeoff run's `/x06/sbg/ekf_nav`
latitude/longitude track over `639_31_50_1971.tif`, whose filename indicates
historical imagery from 1971. Uses rasterio for the georeferenced background and
pyproj to transform longitude/latitude from EPSG:4326 to SWEREF 99 TM (EPSG:3006)
with `always_xy=True`. Marks start/end and shows the track with equal axis scaling
in a full 5 × 5 km overview, with a close-up inset using 15 m padding and a 10 m
scale bar. The script checks that every GPS sample lies inside the raster.
The optional `analysis` dependency group keeps plotting tools separate from decoding.

## Interactive takeoff analysis

From a fresh checkout, initialize the pinned definitions and historical background,
then install and run the locked environment:

```powershell
git submodule update --init --recursive
uv sync --locked --group analysis
uv run --locked --group analysis takeoff-analysis
```

Open **http://localhost:5006** in a browser. The server binds only to localhost;
stop it with Ctrl+C. No notebook state or ROS installation is needed. Run the same
commands on Linux. The local takeoff bag is required and is not downloaded by uv.
For different local paths:

```powershell
uv run --locked --group analysis takeoff-analysis --bag "D:/recordings/takeoff" --raster "D:/maps/639_31_50_1971.tif" --definitions-root "D:/code/FRB-ROS" --output-dir "D:/analysis/exports" --port 5007
```

Defaults resolve from the source checkout, not the shell directory. Explicit
relative paths resolve from the shell directory. A wheel installation outside the
checkout should supply the paths; this workflow is designed for a uv source checkout.

### Explore, select, and export

1. In **01 · Explore**, pan/zoom the enlarged map or choose **Full raster** /
   **Track close-up**. **Selected track** fits the saved GPS selection. Time plots share
   their horizontal range. The box-selection tool is active initially; use the
   toolbar to switch to panning.
2. Drag horizontally on any time plot to preview an interval. Orange points show
   the preview on all views. You may edit the UTC text boundaries at nanosecond
   precision and click **Preview typed bounds**.
3. Enter a **New interval name** and click **Add interval**. If left blank, a numbered
   name is supplied. In **02 · Saved intervals**, select an entry, edit its name,
   and click **Rename**. Expand **Edit exact UTC bounds** to edit boundaries and
   choose **Update interval**, or use **Remove interval**. Add disjoint intervals as
   needed; red points show their union. Names are preserved in the manifest and replay.
4. In **03 · Inspect & export**, inspect selected counts and the paginated table.
   The **Quality flags** tab contains validity plots and counts; **Recording details**
   contains timing and frame assumptions. The table starts with relevant measurements;
   enable **All source columns & exact timestamps** for full detail.
   Choose a source topic to inspect its own native samples. Preview does not affect
   the table or exported selection.
5. Click **Download CSV + manifest (.zip)**. Your browser downloads a ZIP with both
   CSVs and `manifest.json` to its configured download location. Extract it before
   replaying the manifest. Feedback appears directly below the download button;
   a local copy is also retained in a new `takeoff-*` directory under the output
   directory. **Clear selection** clears both saved intervals and preview;
   export stays disabled when no source samples are selected.

The interface uses a dark theme. The map and motion panels sit side by side on wide screens and stack on narrower
screens. Hover shows at most one hit sample per series, with a short UTC time and
formatted measurement instead of a list of nearby rows. This display formatting does
not change export precision.

Intervals include both endpoints: `[start, end]`. Separate intervals remain separate;
the intervening gap is excluded. Overlapping intervals do not duplicate source rows,
but distinct measurements with identical timestamps are retained. Browser drag bounds
are converted from elapsed seconds to integer nanoseconds once, rounding to nearest
nanosecond (ties to even); exact UTC text boundaries are also accepted. Selection and
export use Python source timestamps, never browser datetime values or plot expressions.

### Signals and assumptions

The two required topics are `/x06/sbg/ekf_nav` and `/x06/sbg/ekf_euler`. Their recorded
headers must identify `imu_link_ned`; missing/empty topics or another frame fail clearly.
This is a dataset-specific analysis, not an automatic interpretation of arbitrary bags.

- Position: latitude/longitude in degrees, transformed longitude-first from EPSG:4326
  to EPSG:3006. The historical TIFF must be EPSG:3006. Its image is reduced to 1500 × 1500
  pixels for display; GPS measurements are not downsampled.
- Velocity: sensor NED components in m/s; horizontal speed is `hypot(north, east)`.
  Altitude retains the message definition's mean-sea-level convention in metres.
- Attitude: sensor roll/pitch/yaw in radians, with derived degrees for display. NED
  yaw is about the down axis, zero pointing north. No platform pitch adjustment or
  heading unwrap is applied. Accuracy fields are retained in their native units.
- Timing: bag UTC timestamps drive selection; header timestamps and the sensor's
  uint32 microsecond counter are retained separately. The sensor counter is not
  unwrapped or used for alignment. Time plots use elapsed seconds and state their
  UTC origin and Stockholm recording start.
- Every plot shows native points. There are no connecting lines across gaps or heading
  wraps, interpolation, resampling, quality filtering, or inferred state transitions.
  Validity flags and solution modes are retained as discrete values. Nonfinite GPS
  coordinates are omitted from the map only; source missingness remains in export.

Each CSV contains the analysis scalars for one topic, derived quantities, source row
numbers, exact integer nanoseconds, and ISO-8601 UTC timestamps at the original rate.
An interval can select samples in one topic but none in the other; the latter CSV
still has headers. Empty fields represent missing values. Load integer timestamps
as `int64`, not floating-point epoch seconds; use pandas `float_precision="round_trip"`
when reading CSVs for exact floating-point round trips.

The JSON manifest records intervals, mappings/units, processing settings, source-file
hashes, definition hashes, raster identity, code revision/dirty status, analysis-source
and lockfile hashes, and output counts/hashes. Exports refuse source or code changes
detected since loading. Replay verifies identities and reproduces CSVs into a new directory:

```powershell
uv run --locked --group analysis takeoff-analysis --replay outputs/takeoff-EXAMPLE/manifest.json
```

Replay accepts the same path overrides when files move. Byte-identical inputs,
definitions, analysis source, and lockfile are required. Keep the source checkout
and local data alongside the manifest; the manifest does not contain the original bag.
The Python equivalent is `export_selection(data, intervals, output_dir)` from
`rosbag_decode.analysis.export`, where `data` comes from `analysis.takeoff.load` and
interval boundaries are integer UTC nanoseconds. Pass `interval_names=["Acceleration", "Turn"]`
as an optional keyword to name intervals in Python exports (one name per interval).

### Dependencies and verification

Panel provides controls and the local server; Bokeh provides all interactive plots
and selection events. Their resolved transitive dependencies are recorded in `uv.lock`.
NumPy is now directly declared because analysis and tests import it. Matplotlib,
rasterio, and pyproj remain for the existing map workflow. The decoder's runtime
dependencies are unchanged. No HoloViews, GeoViews, notebook environment, resampling
library, or Parquet dependency is added.

```powershell
uv run --locked --group analysis python -m pytest -q
```

CI is configured for Windows and Linux with Python 3.13.5, pinned FRB definitions,
and small generated bag/raster fixtures. It checks native mappings/timestamps, missing
values, flags and wrapped angles, disjoint/overlapping intervals, exports/replay,
app callbacks, and fresh-process server/document startup. It does not fetch the large
MARV recordings. The real-bag decoder integration test runs locally when its bag exists.

Historical MARV layouts, frame conversions, throttle/heave interpretation, map-based
selection, filtering, and aligned/resampled exports are intentionally deferred.
