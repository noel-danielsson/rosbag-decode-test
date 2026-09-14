"""Native scalar exports with reproducible selection and source identity."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from .selection import interval, mask
from .takeoff import ROOT, SCHEMA_VERSION, load


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def tree_hashes(root, paths):
    return {p.relative_to(root).as_posix(): digest(p) for p in sorted(paths)}


def identity(data):
    return {
        "bag": tree_hashes(data.bag, [p for p in data.bag.rglob("*") if p.is_file()]),
        "raster": digest(data.raster),
        "definitions": tree_hashes(data.definitions, [
            p for p in data.definitions.rglob("*") if p.is_file() and
            (p.suffix == ".msg" or p.name == "package.xml")]),
        "analysis_sources": tree_hashes(ROOT, Path(__file__).parent.glob("*.py")),
        "lockfile": digest(ROOT / "uv.lock"),
    }


def git_state():
    def git(*args):
        result = subprocess.run(["git", "-c", f"safe.directory={ROOT.as_posix()}",
                                 "-C", str(ROOT), *args], capture_output=True, text=True)
        return result.stdout.strip() if result.returncode == 0 else None
    status = git("status", "--porcelain")
    return {"revision": git("rev-parse", "HEAD"),
            "dirty": status != "" if status is not None else None}


def processing(data):
    return {"boundaries": "inclusive [start, end]; union without duplicated source rows",
            "timestamp_source": "integer bag recording timestamps, UTC nanoseconds",
            "frame": "imu_link_ned; sensor attitude, no FRB pitch adjustment",
            "crs": "EPSG:3006", "input_crs": "EPSG:4326",
            "interpolation": "none", "resampling": "none", "filtering": "none",
            "missing_values": "CSV empty fields; native missingness retained",
            "origin_ns": str(data.origin_ns)}


def export_selection(data, intervals, output_dir, *, interval_names=None):
    intervals = [interval(*bounds) for bounds in intervals]
    if not intervals:
        raise ValueError("Add at least one interval before exporting")
    names = ([f"Interval {i+1}" for i in range(len(intervals))]
             if interval_names is None else list(interval_names))
    if len(names) != len(intervals) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("Provide one nonempty name per interval")
    names = [name.strip() for name in names]
    selected = {topic: frame.loc[mask(frame, intervals)] for topic, frame in data.frames.items()}
    if not any(len(frame) for frame in selected.values()):
        raise ValueError("Selected intervals contain no measurements")
    # Hash before creating outputs, so failures leave no partial CSV bundle.
    source_identity = identity(data)
    if data.source_identity is not None and source_identity != data.source_identity:
        raise ValueError("Source files or analysis code changed since loading; restart the analysis before exporting")
    output_dir = Path(output_dir).resolve()
    if output_dir == data.bag or data.bag in output_dir.parents:
        raise ValueError("Export directory must be outside the source bag")
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = Path(tempfile.mkdtemp(prefix="takeoff-", dir=output_dir))
    files = {}
    for topic, frame in selected.items():
        filename = topic.rsplit("/", 1)[-1] + ".csv"
        frame.to_csv(destination / filename, index=False, float_format="%.17g", lineterminator="\n")
        files[topic] = {"file": filename, "rows": len(frame), "sha256": digest(destination / filename)}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "sources": {"bag": str(data.bag), "raster": str(data.raster), "definitions": str(data.definitions)},
        "identity": source_identity,
        "code": git_state(),
        "intervals": [{"name": name, "start_ns": str(start), "end_ns": str(end)}
                      for name, (start, end) in zip(names, intervals)],
        "processing": processing(data),
        "timing_columns": {"timestamp_ns": "bag UTC ns", "timestamp_utc": "bag ISO-8601 UTC",
                           "header_timestamp_ns": "header sec * 1e9 + nanosec; not used for selection",
                           "sensor_timestamp_us": "uint32 microseconds since sensor startup; not unwrapped",
                           "source_row": "zero-based row within decoded source topic"},
        "mappings": data.mappings,
        "outputs": files,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return destination


def replay(manifest_path, output_dir, *, bag=None, raster=None, definitions=None):
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError("Unsupported analysis schema version")
    sources = manifest["sources"]
    data = load(bag or sources["bag"], raster or sources["raster"], definitions or sources["definitions"])
    if (data.source_identity != manifest["identity"] or data.mappings != manifest["mappings"]
            or processing(data) != manifest["processing"]):
        raise ValueError("Source files, definitions, analysis code, mappings, or lockfile changed; replay refused")
    intervals = [(int(i["start_ns"]), int(i["end_ns"])) for i in manifest["intervals"]]
    names = [item.get("name", f"Interval {i+1}") for i, item in enumerate(manifest["intervals"])]
    destination = export_selection(data, intervals, output_dir, interval_names=names)
    reproduced = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    if reproduced["processing"] != manifest["processing"] or reproduced["outputs"] != manifest["outputs"]:
        raise ValueError(f"Replay output differs from manifest; inspect {destination}")
    return destination
