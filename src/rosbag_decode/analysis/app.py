"""Small Panel/Bokeh interface; selections live in Python, not plot expressions."""
import math
from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED
from html import escape

import numpy as np
import panel as pn
from bokeh.events import SelectionGeometry
from bokeh.models import BoxAnnotation, BoxSelectTool, ColumnDataSource, HoverTool, Range1d, NumeralTickFormatter
from bokeh.plotting import figure

from .export import export_selection
from .selection import from_drag, interval, mask, parse_utc, utc
from .takeoff import NAV, EULER

BLUE = "#55bfd7"
SELECTED = "#da6842"
PREVIEW = "#dfaa32"
CARD = {"background": "#18232f", "border": "1px solid #334353", "border-radius": "14px",
        "padding": "18px", "box-sizing": "border-box", "min-width": "0"}
BUTTON_CSS = """
button {font-family: Inter, 'Segoe UI', sans-serif; font-size: 13px; border-radius: 7px;
        padding: 8px 14px; box-shadow: none;}
"""


def button(label, **kwargs):
    return pn.widgets.Button(label=label, stylesheets=[BUTTON_CSS], margin=0, **kwargs)


def on_click(widget, action):
    # Panel widget events run outside the Bokeh document lock by default.
    # These actions also update Bokeh sources/ranges, so acquire that lock.
    @pn.io.with_lock
    async def handle(event):
        action()
    widget.on_click(handle)


def heading(title, subtitle):
    return pn.pane.HTML(f'<div style="font-size:17px;font-weight:650;color:#e6edf5">{escape(title)}</div>'
                        f'<div style="font-size:12px;color:#a3b3c4;margin-top:4px">{escape(subtitle)}</div>', margin=(0, 0, 12, 0))


def style_plot(plot):
    plot.toolbar.logo = None
    plot.background_fill_color = "#18232f"
    plot.border_fill_color = "#18232f"
    plot.outline_line_color = None
    plot.grid.grid_line_color = "#2c3948"
    plot.axis.axis_line_color = "#435467"
    plot.axis.major_tick_line_color = None
    plot.axis.minor_tick_line_color = None
    plot.axis.major_label_text_font = "Segoe UI"
    plot.axis.major_label_text_font_size = "11px"
    plot.axis.major_label_text_color = "#a3b3c4"
    plot.axis.axis_label_text_font_style = "normal"
    plot.axis.axis_label_text_font_size = "11px"
    plot.axis.axis_label_text_color = "#a3b3c4"
    plot.title.text_font = "Segoe UI"
    plot.title.text_font_size = "13px"
    plot.title.text_color = "#d5e0eb"
    plot.min_border_left = 48
    plot.min_border_right = 10


class TakeoffApp:
    def __init__(self, data, output_dir):
        self.data, self.output_dir = data, output_dir
        self.intervals, self.preview = [], None
        self.interval_names = []
        self.next_interval_number = 1
        self.plot_layers = []
        self.annotations = []
        self.display_times = {topic: frame.index.strftime("%H:%M:%S.%f").str.slice(0, 12).to_numpy()
                              for topic, frame in data.frames.items()}
        self.message = pn.pane.Markdown(margin=0, styles={"font-size": "13px", "color": "#b6c5d4", "flex": "1 1 360px"})
        self.start = pn.widgets.TextInput(label="Start UTC · inclusive", sizing_mode="stretch_width", min_width=220)
        self.end = pn.widgets.TextInput(label="End UTC · inclusive", sizing_mode="stretch_width", min_width=220)
        self.add = button("Add interval", color="primary", disabled=True)
        self.interval_name = pn.widgets.TextInput(label="New interval name", placeholder="e.g. Initial acceleration", width=230, margin=0)
        self.saved_name = pn.widgets.TextInput(label="Interval name", sizing_mode="stretch_width")
        self.rename = button("Rename")
        self.apply = button("Preview typed bounds")
        self.saved = pn.widgets.Select(label="Saved intervals", options={}, sizing_mode="stretch_width")
        self.update = button("Update interval")
        self.remove = button("Remove interval")
        self.clear = button("Clear selection")
        self.export = pn.widgets.FileDownload(label="Download CSV + manifest (.zip)",
            callback=self.download_current, filename="takeoff-selection.zip", color="primary",
            disabled=True, margin=0, stylesheets=[BUTTON_CSS])
        self.export_status = pn.pane.Markdown(
            "Downloads a ZIP containing both CSVs and the manifest.", margin=(6, 0, 8, 0),
            styles={"font-size": "12px", "overflow-wrap": "anywhere"})
        self.summary = pn.pane.HTML(margin=(4, 0, 10, 0), sizing_mode="stretch_width")
        self.quality_summary = pn.pane.Markdown(styles={"font-size": "12px"})
        self.topic = pn.widgets.Select(label="Measurements", options={"Navigation": NAV, "Attitude": EULER}, width=180)
        self.page = pn.widgets.IntInput(label="Page (50 rows)", value=1, start=1, end=1, width=150)
        self.all_columns = pn.widgets.Checkbox(label="All source columns & exact timestamps", value=False)
        self.table = pn.pane.HTML(height=300, sizing_mode="stretch_width",
                                  styles={"overflow": "auto", "min-width": "0"}, margin=0)
        on_click(self.add, self.add_interval)
        on_click(self.apply, self.typed_preview)
        on_click(self.update, self.update_interval)
        on_click(self.remove, self.remove_interval)
        on_click(self.rename, self.rename_interval)
        on_click(self.clear, self.clear_selection)
        self.saved.param.watch(self.edit_interval, "value")
        self.topic.param.watch(lambda _: self.refresh_table(), "value")
        self.page.param.watch(lambda _: self.refresh_table(), "value")
        self.all_columns.param.watch(lambda _: self.refresh_table(), "value")

        nav = data.frames[NAV]
        left, bottom, right, top = data.bounds
        self.map = figure(name="gps-map", sizing_mode="scale_width", width=600, height=600, aspect_ratio=1,
                          x_range=(left, right), y_range=(bottom, top), match_aspect=True,
                          tools="pan,wheel_zoom,reset", active_scroll="wheel_zoom", toolbar_location=None,
                          min_border=0)
        self.map.axis.visible = False
        self.map.grid.visible = False
        self.map.outline_line_color = None
        self.map.image(image=[np.flipud(data.background)], x=left, y=bottom,
                       dw=right-left, dh=top-bottom, palette="Greys256")
        self.add_layer(self.map, NAV, "easting", "northing", "Position", color="#35c3e7", unit="m")
        finite = np.isfinite(nav.easting) & np.isfinite(nav.northing)
        self.finite_nav = nav.loc[finite]
        if len(self.finite_nav):
            first, last = self.finite_nav.iloc[0], self.finite_nav.iloc[-1]
            self.map.scatter([first.easting], [first.northing], size=10, color="#5ee38a", line_color="white", marker="circle")
            self.map.scatter([last.easting], [last.northing], size=12, color=SELECTED, line_color="white", marker="diamond")
        full = button("Full raster")
        track = button("Track close-up")
        selected_track = button("Selected track")
        on_click(full, lambda: self.map_bounds(left, bottom, right, top))
        on_click(track, self.track_bounds)
        on_click(selected_track, lambda: self.track_bounds(selected=True))
        self.selected_track = selected_track
        end_seconds = max((frame.index.as_unit("ns").asi8.max() - data.origin_ns) / 1e9 for frame in data.frames.values())
        shared = Range1d(0, float(end_seconds))
        plots = []
        for title, topic, signals, unit in [
            ("Horizontal speed", NAV, ["horizontal_speed"], "m/s"),
            ("Roll & pitch", EULER, ["roll_deg", "pitch_deg"], "°"),
            ("Yaw", EULER, ["yaw_deg"], "°"),
            ("Navigation validity", NAV, ["position_valid", "velocity_valid", "heading_valid", "attitude_valid"], ""),
        ]:
            plot = figure(title=title, name=title, height=185, sizing_mode="stretch_width", x_range=shared,
                          tools="xpan,xwheel_zoom,reset", active_scroll="xwheel_zoom")
            style_plot(plot)
            box = BoxSelectTool(dimensions="width", renderers=[])
            plot.add_tools(box)
            plot.toolbar.active_drag = box
            plot.on_event(SelectionGeometry, self.drag)
            plot.yaxis.axis_label = unit
            plot.yaxis.formatter = NumeralTickFormatter(format="0.[00]")
            plot.xaxis.axis_label = "Elapsed time (s)" if title in ("Yaw", "Navigation validity") else None
            labels = {"horizontal_speed": "Speed", "roll_deg": "Roll", "pitch_deg": "Pitch", "yaw_deg": "Yaw",
                      "position_valid": "Position", "velocity_valid": "Velocity", "heading_valid": "Heading", "attitude_valid": "Attitude"}
            for number, signal in enumerate(signals):
                self.add_layer(plot, topic, "elapsed", signal, labels[signal],
                               [BLUE, "#c795e8", "#ddbc66", "#80c6a4"][number], unit=unit)
            preview_box = BoxAnnotation(fill_color=PREVIEW, fill_alpha=0.10, visible=False)
            plot.add_layout(preview_box)
            self.annotations.append((plot, preview_box, []))
            if len(signals) == 1:
                plot.legend.visible = False
            else:
                plot.legend.orientation = "horizontal"
                plot.legend.location = "top_left"
                plot.legend.label_text_font_size = "10px"
                plot.legend.label_text_color = "#b6c5d4"
                plot.legend.border_line_color = None
                plot.legend.background_fill_color = "#18232f"
                plot.legend.background_fill_alpha = 0.85
                plot.legend.padding = 3
                plot.legend.spacing = 8
            plots.append(plot)
        self.time_plots = plots
        local = data.frames[NAV].index.min().tz_convert("Europe/Stockholm")
        end_local = data.frames[NAV].index.max().tz_convert("Europe/Stockholm")
        header = pn.pane.HTML(
            '<div style="color:#55bfd7;font-size:11px;font-weight:700;letter-spacing:1.5px;margin-bottom:7px">MARINE ANALYSIS</div>'
            '<div style="font-size:30px;font-weight:650;letter-spacing:-.7px;color:#e6edf5">Takeoff run</div>'
            f'<div style="color:#a3b3c4;font-size:13px;margin-top:7px">{local:%d %B %Y} &nbsp;·&nbsp; '
            f'{local:%H:%M}–{end_local.ceil("min"):%H:%M} {local:%Z} &nbsp;·&nbsp; '
            f'{int(end_seconds)//60} min {round(end_seconds)%60:02d} s recording</div>', margin=0)
        map_card = pn.Column(
            pn.FlexBox(heading("GPS track", "Pan the map or scroll to zoom"),
                       pn.FlexBox(full, track, selected_track, gap="6px", styles={"flex": "0 1 auto"}),
                       justify_content="space-between", align_items="center", gap="8px"),
            self.map,
            pn.pane.HTML('<div style="font-size:11px;color:#a3b3c4;padding-top:10px">'
                         '<span style="color:#269cbe">● Track</span> &nbsp; <span style="color:#42b96a">● Start</span> &nbsp; '
                         '<span style="color:#da6842">◆ End / selection</span> &nbsp;·&nbsp; North is up<br>'
                         'Historical aerial imagery · 1971 · SWEREF 99 TM</div>', margin=0),
            sizing_mode="stretch_width", margin=0, styles={**CARD, "flex": "1 1 550px"})
        motion_card = pn.Column(heading("Motion", "Drag horizontally to preview an interval · scroll to zoom"),
                                *plots[:3], sizing_mode="stretch_width", margin=0,
                                styles={**CARD, "flex": "1 1 550px"})
        exact = pn.Column(pn.FlexBox(self.start, self.end, gap="12px"),
                          pn.FlexBox(self.apply, self.update, gap="8px"), sizing_mode="stretch_width")
        inspection = pn.Column(pn.FlexBox(self.topic, self.page, self.all_columns, gap="12px", align_items="center"),
                               self.table, sizing_mode="stretch_width")
        details = pn.pane.Markdown(
            f"**Bag time origin (UTC):** `{utc(data.origin_ns)}`\n\n"
            f"**Background:** `{data.raster.name}` — its filename indicates historical imagery from 1971.\n\n"
            "Native measurements are preserved. No interpolation, resampling, heading unwrap, or quality filtering. "
            "Sensor attitude and velocity use the recorded NED frame. Both selection endpoints are inclusive; "
            "separate intervals exclude the intervening gaps.\n\n"
            f"Nonfinite GPS rows omitted from map: {len(nav)-int(finite.sum())}.", styles={"font-size": "12px"})
        selection_card = pn.Column(
            heading("02 · Saved intervals", "Name each part of the recording you want to keep"),
            self.saved,
            pn.FlexBox(self.saved_name, self.rename, self.remove, self.clear, gap="10px", align_items="end"),
            pn.Accordion(("Edit exact UTC bounds", exact), active=[], sizing_mode="stretch_width"),
            sizing_mode="stretch_width", margin=0, styles=CARD)
        inspect_card = pn.Column(
            pn.FlexBox(heading("03 · Inspect & export", "Native samples from your saved intervals · gaps stay excluded"), self.export,
                       justify_content="space-between", align_items="center"), self.export_status, self.summary,
            pn.Tabs(("Selected samples", inspection), ("Quality flags", pn.Column(plots[3], self.quality_summary)),
                    ("Recording details", details), sizing_mode="stretch_width", dynamic=False),
            sizing_mode="stretch_width", margin=0, styles=CARD)
        self.view = pn.Column(
            header,
            pn.Spacer(height=20), heading("01 · Explore", "Compare the track and motion, then drag across a time plot to preview a range"),
            pn.FlexBox(self.message, self.interval_name, self.add, gap="12px", align_items="center",
                       styles={**CARD, "padding": "12px 18px"}, margin=(20, 0, 16, 0)),
            pn.FlexBox(map_card, motion_card, gap="18px", align_items="stretch", sizing_mode="stretch_width", margin=0),
            pn.Spacer(height=16), selection_card, pn.Spacer(height=16), inspect_card,
            sizing_mode="stretch_width", margin=0,
            styles={"background": "#101821", "padding": "24px", "box-sizing": "border-box",
                    "font-family": "Inter, Segoe UI, sans-serif", "color": "#e6edf5"},
        )
        self.refresh()

    def source(self, topic, x, y, selected=None):
        frame = self.data.frames[topic]
        if selected is not None:
            frame = frame.loc[selected]
        xx = ((frame.index.as_unit("ns").asi8 - self.data.origin_ns) / 1e9
              if x == "elapsed" else frame[x].to_numpy())
        yy = frame[y].to_numpy()
        finite = np.isfinite(xx) & np.isfinite(yy)
        return {"x": xx[finite], "y": yy[finite],
                "time": self.display_times[topic][frame.source_row.to_numpy()[finite]]}

    def add_layer(self, plot, topic, x, y, label, color=BLUE, unit=""):
        base = ColumnDataSource(self.source(topic, x, y))
        kwargs = {} if plot is self.map else {"legend_label": label}
        glyph = plot.scatter("x", "y", source=base, size=3, color=color, alpha=0.8, **kwargs)
        tips = [("Time (UTC)", "@time"), (label, f"@y{{0.000}} {unit}".strip())]
        if plot is self.map:
            tips = [("Time (UTC)", "@time"), ("Easting", "@x{0,0.0} m"), ("Northing", "@y{0,0.0} m")]
        plot.add_tools(HoverTool(renderers=[glyph], tooltips=tips, limit=1, mode="mouse",
                                 point_policy="follow_mouse", show_arrow=False))
        saved, preview = ColumnDataSource(dict(x=[], y=[], time=[])), ColumnDataSource(dict(x=[], y=[], time=[]))
        plot.scatter("x", "y", source=saved, size=4, color=SELECTED)
        plot.scatter("x", "y", source=preview, size=4, color=PREVIEW)
        self.plot_layers.append((topic, x, y, saved, preview))

    def map_bounds(self, left, bottom, right, top):
        self.map.x_range.start, self.map.x_range.end = float(left), float(right)
        self.map.y_range.start, self.map.y_range.end = float(bottom), float(top)

    def track_bounds(self, selected=False):
        nav = self.finite_nav
        if selected:
            nav = nav.loc[mask(nav, self.intervals)]
        if len(nav):
            center_x, center_y = (nav.easting.min()+nav.easting.max())/2, (nav.northing.min()+nav.northing.max())/2
            half = max(nav.easting.max()-nav.easting.min(), nav.northing.max()-nav.northing.min())/2 + 20
            self.map_bounds(center_x-half, center_y-half, center_x+half, center_y+half)

    def drag(self, event):
        if event.final and event.geometry.get("type") == "rect":
            self.set_preview(from_drag(event.geometry["x0"], event.geometry["x1"], self.data.origin_ns))

    def set_preview(self, bounds):
        self.preview = interval(*bounds)
        self.start.value, self.end.value = map(utc, self.preview)
        self.refresh()

    def typed_preview(self):
        try:
            self.set_preview((parse_utc(self.start.value), parse_utc(self.end.value)))
            return True
        except (ValueError, TypeError) as exc:
            self.message.object = f"**Invalid interval:** {exc}"
            return False

    def add_interval(self):
        if self.typed_preview():
            self.intervals.append(self.preview)
            self.interval_names.append(self.interval_name.value.strip() or f"Interval {self.next_interval_number}")
            self.next_interval_number += 1
            self.interval_name.value = ""
            self.preview = None
            self.refresh()

    def edit_interval(self, event):
        if event.new is not None and event.new < len(self.intervals):
            self.start.value, self.end.value = map(utc, self.intervals[event.new])
            self.saved_name.value = self.interval_names[event.new]

    def rename_interval(self):
        if self.saved.value is not None:
            name = self.saved_name.value.strip()
            if not name:
                self.message.object = "Enter a name before renaming the interval."
                return
            self.interval_names[self.saved.value] = name
            self.refresh()

    def update_interval(self):
        index = self.saved.value
        if index is not None and self.typed_preview():
            self.intervals[index] = self.preview
            self.preview = None
            self.refresh()

    def remove_interval(self):
        if self.saved.value is not None:
            index = self.saved.value
            self.intervals.pop(index)
            self.interval_names.pop(index)
            self.refresh()
            if self.saved.value is not None:
                self.saved_name.value = self.interval_names[self.saved.value]

    def clear_selection(self):
        self.intervals.clear()
        self.interval_names.clear()
        self.preview = None
        self.start.value = self.end.value = ""
        self.saved_name.value = self.interval_name.value = ""
        self.refresh()

    def refresh(self):
        counts = {}
        for topic, x, y, saved, preview in self.plot_layers:
            frame = self.data.frames[topic]
            saved.data = self.source(topic, x, y, mask(frame, self.intervals))
            preview.data = self.source(topic, x, y, mask(frame, [self.preview] if self.preview else []))
        for plot, preview_box, old in self.annotations:
            for box in old:
                plot.center.remove(box)
            old.clear()
            for start, end in self.intervals:
                box = BoxAnnotation(left=(start-self.data.origin_ns)/1e9, right=(end-self.data.origin_ns)/1e9,
                                    fill_color=SELECTED, fill_alpha=0.08)
                plot.add_layout(box)
                old.append(box)
            preview_box.visible = self.preview is not None
            if self.preview:
                preview_box.left, preview_box.right = [(v-self.data.origin_ns)/1e9 for v in self.preview]
        self.saved.options = {f"{i+1}. {self.interval_names[i]}  ·  {utc(a)[11:23]} – {utc(b)[11:23]} UTC  ·  {(b-a)/1e9:.2f} s": i
                              for i, (a, b) in enumerate(self.intervals)}
        if self.intervals and self.saved.value is None:
            self.saved.value = 0
        if self.saved.value is None:
            self.saved_name.value = ""
        for topic, frame in self.data.frames.items():
            subset = frame.loc[mask(frame, self.intervals)]
            flags = [c for c in subset if c.endswith("_valid")]
            invalid = ", ".join(f"{flag}: {int((~subset[flag]).sum())} false" for flag in flags)
            counts[topic] = len(subset)
            counts[topic + "_text"] = f"**{topic}:** {len(subset):,} selected / {len(frame):,} native samples · {invalid}"
        self.quality_summary.object = "\n\n".join(v for k, v in counts.items() if k.endswith("_text"))
        self.summary.object = '<div style="display:flex;gap:32px;flex-wrap:wrap;padding:4px 0 8px">' + ''.join(
            f'<div><div style="font-size:24px;font-weight:600;color:#e6edf5">{value:,}</div>'
            f'<div style="font-size:12px;color:#a3b3c4">{label}</div></div>'
            for label, value in [("Saved intervals", len(self.intervals)), ("Navigation samples", counts[NAV]),
                                 ("Attitude samples", counts[EULER])]) + '</div>'
        self.export.disabled = not any(v for k, v in counts.items() if not k.endswith("_text"))
        self.selected_track.disabled = counts[NAV] == 0
        self.add.disabled = self.preview is None
        for widget in (self.rename, self.remove, self.update, self.saved_name, self.saved):
            widget.disabled = not self.intervals
        self.clear.disabled = not self.intervals and self.preview is None
        if self.preview:
            duration = (self.preview[1]-self.preview[0])/1e9
            self.message.object = f"**Preview · {duration:.2f} s**  \n{utc(self.preview[0])[11:23]} – {utc(self.preview[1])[11:23]} UTC · Name it and add to your selection."
        else:
            self.message.object = "**Preview an interval**  \nDrag across a time plot. Your saved intervals stay highlighted."
        self.refresh_table()

    def refresh_table(self):
        frame = self.data.frames[self.topic.value]
        subset = frame.loc[mask(frame, self.intervals)]
        self.page.end = max(1, math.ceil(len(subset)/50))
        if self.page.value > self.page.end:
            self.page.value = self.page.end
        page = subset.iloc[(self.page.value-1)*50:self.page.value*50].reset_index(drop=True).copy()
        for column in ("timestamp_ns", "header_timestamp_ns"):
            page[column] = page[column].astype(str)
        if not self.all_columns.value:
            page["Time (UTC)"] = page.timestamp_utc.str.slice(11, 23)
            columns = (["horizontal_speed", "latitude", "longitude"] if self.topic.value == NAV else
                       ["roll_deg", "pitch_deg", "yaw_deg"])
            page = page[["Time (UTC)", *columns, "source_row"]].rename(columns={
                "horizontal_speed": "Speed (m/s)", "latitude": "Latitude (°)", "longitude": "Longitude (°)",
                "roll_deg": "Roll (°)", "pitch_deg": "Pitch (°)", "yaw_deg": "Yaw (°)", "source_row": "Source row"})
        self.table_frame = page
        if page.empty:
            self.table.object = '<div style="padding:36px;text-align:center;color:#a3b3c4;font-size:13px">'
            self.table.object += 'No samples selected yet. Add an interval in Explore to inspect it here.</div>'
        else:
            self.table.object = '<style>table{border-collapse:collapse;width:100%;font-family:Segoe UI,sans-serif;font-size:12px;white-space:nowrap}'
            self.table.object += 'th{background:#101821;color:#b6c5d4;text-align:left!important;position:sticky;top:0}'
            self.table.object += 'td,th{padding:9px 12px;border:0;border-bottom:1px solid #2c3948}td{color:#d5e0eb}</style>'
            self.table.object += page.to_html(index=False, border=0, escape=True, na_rep="—",
                                              float_format=lambda v: f"{v:.6f}".rstrip("0").rstrip("."))

    def export_current(self):
        try:
            result = export_selection(self.data, self.intervals, self.output_dir, interval_names=self.interval_names)
            self.export_status.object = f"**Export saved:** `{result}`"
            return result
        except (ValueError, OSError) as exc:
            self.export_status.object = f"**Export failed:** {exc}"

    def download_current(self):
        result = self.export_current()
        if result is None:
            # Stop the transfer rather than download an empty or previous bundle.
            raise ValueError("Export failed; see the message beside the download button")
        try:
            buffer = BytesIO()
            with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as archive:
                for path in sorted(result.iterdir()):
                    archive.write(path, arcname=path.name)
            buffer.seek(0)
            self.export.filename = f"{result.name}.zip"
            self.export_status.object = (
                "**Download prepared.** Check your browser’s downloads. "
                f"A local copy is also saved in `{result}`.")
            return buffer
        except OSError as exc:
            self.export_status.object = f"**Download failed:** {exc}"
            raise


def create_app(data, output_dir):
    pn.extension(theme="dark")
    return TakeoffApp(data, output_dir)
