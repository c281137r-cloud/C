"""ffmpeg を使って録画からフレーム（静止画）を切り出す。"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Frame:
    t: float  # 動画内の秒数
    path: Path

    @property
    def label(self) -> str:
        return fmt_ts(self.t)


def fmt_ts(sec: float) -> str:
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def probe_duration(video: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(video)],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def plan_interval(duration: float, interval: float, max_frames: int) -> float:
    """フレーム数が max_frames を超えないよう、必要なら間隔を広げる。"""
    if duration / interval > max_frames:
        return duration / max_frames
    return interval


def extract_frames(
    video: Path,
    out_dir: Path,
    interval: float,
    width: int,
    start: float = 0.0,
    end: float | None = None,
) -> list[Frame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-v", "error", "-y"]
    if start:
        cmd += ["-ss", str(start)]
    if end is not None:
        cmd += ["-to", str(end)]
    cmd += [
        "-i", str(video),
        "-vf", f"fps=1/{interval},scale={width}:-2",
        "-q:v", "4",
        str(out_dir / "f_%05d.jpg"),
    ]
    subprocess.run(cmd, check=True)
    files = sorted(out_dir.glob("f_*.jpg"))
    return [Frame(t=start + i * interval, path=p) for i, p in enumerate(files)]


def chunk(frames: list[Frame], size: int) -> list[list[Frame]]:
    return [frames[i : i + size] for i in range(0, len(frames), size)]
