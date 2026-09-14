"""Run from any directory with uv run --locked examples/takeoff.py."""
from pathlib import Path

from rosbag_decode import bag2dfs, list_topics, topic2df

bag = Path(__file__).resolve().parents[1] / "rosbag2_2025_11_14-14_23_19_foil_i_takeoff"
print(list_topics(bag).to_string(index=False))
topic = "/x06/frb/nav/sbg_pose"
pose = topic2df(bag, topic)
latitude = pose[f"{topic}/pos_geo"].map(lambda position: position["x"])
print("Latitude from nested pos_geo dictionaries:")
print(latitude.head())
frames = bag2dfs(bag)
print(f"Loaded {sum(map(len, frames.values())):,} rows across {len(frames)} populated topics")
