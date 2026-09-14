"""A fresh-process entrypoint for the local takeoff app and manifest replay."""
import argparse
from pathlib import Path

from .takeoff import DEFAULT_BAG, DEFAULT_RASTER, ROOT, load


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bag", type=Path)
    parser.add_argument("--raster", type=Path)
    parser.add_argument("--definitions-root", type=Path)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "outputs")
    parser.add_argument("--port", type=int, default=5006)
    parser.add_argument("--replay", type=Path, metavar="MANIFEST")
    args = parser.parse_args()
    try:
        if args.replay:
            from .export import replay
            print(replay(args.replay, args.output_dir, bag=args.bag, raster=args.raster,
                         definitions=args.definitions_root))
            return
        import panel as pn
        from .app import create_app
        data = load(args.bag or DEFAULT_BAG, args.raster or DEFAULT_RASTER,
                    args.definitions_root or ROOT / "FRB-ROS")
        print(f"Takeoff analysis: http://localhost:{args.port} (Ctrl+C to stop)", flush=True)
        # Bokeh 3.9's multi-root static handler skips Tornado's initializer.
        # Serve Panel's installed assets through the regular static handler so
        # Tornado's path/symlink validation is initialized and remains enabled.
        panel_assets = Path(pn.__file__).resolve().parent / "dist"
        pn.serve(lambda: create_app(data, args.output_dir).view, address="127.0.0.1",
                 port=args.port, show=False, title="Takeoff analysis",
                 static_dirs={"/static/extensions/panel": str(panel_assets)})
    except (ValueError, FileNotFoundError) as exc:
        parser.exit(2, f"Analysis error: {exc}\n")


if __name__ == "__main__":
    main()
