"""Plot the takeoff GPS track over the historical 1971 GeoTIFF."""
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patheffects as path_effects
import numpy as np
from pyproj import Transformer
import rasterio
from rasterio.plot import plotting_extent
from rasterio.windows import Window, from_bounds

from rosbag_decode import topic2df

ROOT = Path(__file__).resolve().parents[1]
BAG = ROOT / "rosbag2_2025_11_14-14_23_19_foil_i_takeoff"
RASTER = ROOT / "MARV-Test-Data/rosbag_decode/geo-data/639_31_50_1971.tif"
OUTPUT = ROOT / "outputs/takeoff_gps_track.png"
TOPIC = "/x06/sbg/ekf_nav"


def main():
    gps = topic2df(BAG, TOPIC)
    longitude = gps[f"{TOPIC}/longitude"].to_numpy()
    latitude = gps[f"{TOPIC}/latitude"].to_numpy()
    if not len(gps) or not (np.isfinite(longitude).all() and np.isfinite(latitude).all()):
        raise ValueError("GPS topic must contain finite latitude/longitude samples")

    with rasterio.open(RASTER) as raster:
        if raster.crs is None or raster.crs.to_epsg() != 3006:
            raise ValueError(f"Expected SWEREF 99 TM (EPSG:3006), got {raster.crs}")
        transformer = Transformer.from_crs("EPSG:4326", raster.crs, always_xy=True)
        east, north = transformer.transform(longitude, latitude, errcheck=True)
        bounds = raster.bounds
        inside = ((east >= bounds.left) & (east <= bounds.right)
                  & (north >= bounds.bottom) & (north <= bounds.top))
        if not inside.all():
            raise ValueError(f"{np.count_nonzero(~inside)} GPS samples lie outside the GeoTIFF")
        padding = 15.0
        left, right = east.min() - padding, east.max() + padding
        bottom, top = north.min() - padding, north.max() + padding
        # Read only the visible area, keeping the source pixels and georeferencing.
        window = from_bounds(left, bottom, right, top, raster.transform)
        window = window.round_offsets().round_lengths().intersection(
            Window(0, 0, raster.width, raster.height))
        background = raster.read(1, window=window, masked=True)
        extent = plotting_extent(background, raster.window_transform(window))
        overview = raster.read(1, out_shape=(2000, 2000), masked=True)
        overview_extent = (bounds.left, bounds.right, bounds.bottom, bounds.top)

    fig, ax = plt.subplots(figsize=(11, 11))
    fig.subplots_adjust(top=0.90, bottom=0.13, left=0.13, right=0.97)
    ax.imshow(overview, extent=overview_extent, origin="upper", cmap="gray", vmin=0, vmax=255)
    ax.plot(east, north, color="#00c5ff", linewidth=2, zorder=3)
    ax.scatter(east[0], north[0], s=45, facecolors="none", edgecolors="#00c5ff", zorder=4)
    closeup = ax.inset_axes([0.56, 0.54, 0.41, 0.42])
    closeup.imshow(background, extent=extent, origin="upper", cmap="gray", vmin=0, vmax=255)
    track, = closeup.plot(east, north, color="#00c5ff", linewidth=1.5, label="GPS track", zorder=3)
    track.set_path_effects([path_effects.Stroke(linewidth=2.8, foreground="#12303c"),
                            path_effects.Normal()])
    closeup.scatter(east[0], north[0], marker="o", s=80, color="#39e75f",
               edgecolors="black", linewidths=1.2, label="Start", zorder=5)
    closeup.scatter(east[-1], north[-1], marker="X", s=90, color="#ff674f",
               edgecolors="black", linewidths=1.2, label="End", zorder=6)
    closeup.set(xlim=(left, right), ylim=(bottom, top), xticks=[], yticks=[])
    closeup.set_aspect("equal", adjustable="box")
    closeup.set_title("Track close-up · 15 m padding", fontsize=10, backgroundcolor="white")
    closeup.legend(loc="upper left", fontsize=8, framealpha=0.95)
    closeup.plot([left + 5, left + 15], [bottom + 5, bottom + 5], color="white", linewidth=3)
    closeup.text(left + 10, bottom + 7, "10 m", ha="center", color="white", fontsize=9)
    for spine in closeup.spines.values():
        spine.set_edgecolor("#00a5d5")
        spine.set_linewidth(2)
    ax.indicate_inset_zoom(closeup, edgecolor="#00c5ff", alpha=1, linewidth=1.5)
    ax.set(xlim=(bounds.left, bounds.right), ylim=(bounds.bottom, bounds.top),
           xlabel="Easting (m) · SWEREF 99 TM", ylabel="Northing (m) · SWEREF 99 TM")
    ax.set_aspect("equal", adjustable="box")
    ax.ticklabel_format(style="plain", useOffset=False)
    ax.tick_params(axis="x", rotation=25)
    fig.suptitle("Takeoff run — 14 November 2025, 14:23–14:28 CET", fontsize=14)
    fig.text(0.5, 0.035,
             "Background: 639_31_50_1971.tif — filename indicates historical imagery from 1971.\n"
             "GPS: /x06/sbg/ekf_nav · Full 5 × 5 km overview with track close-up · SWEREF 99 TM",
             ha="center", fontsize=9, color="#444444")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUTPUT, dpi=180)
    plt.close(fig)
    print(f"Saved {OUTPUT}")
    print(f"All {len(gps):,} GPS samples lie inside the raster.")
    print(f"Track bounds: E {east.min():,.2f}–{east.max():,.2f} m; "
          f"N {north.min():,.2f}–{north.max():,.2f} m")


if __name__ == "__main__":
    main()
