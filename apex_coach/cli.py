"""コマンドライン入口。

  python -m apex_coach analyze 録画.mp4        # 1本をコーチング
  python -m apex_coach analyze 録画.mp4 --dry-run   # 費用見積もりだけ
  python -m apex_coach watch ./inbox           # フォルダを監視して自動コーチング
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import shutil
import sys
import time
import tomllib
from dataclasses import fields
from pathlib import Path

from .coach import Coach, Settings, estimate

VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov"}


# --- 設定 -------------------------------------------------------------------
def load_config(path: Path | None) -> tuple[Settings, str]:
    s = Settings()
    profile = ""
    if path and path.exists():
        cfg = tomllib.loads(path.read_text(encoding="utf-8"))
        names = {f.name for f in fields(Settings)}
        for k, v in cfg.get("coach", {}).items():
            if k in names:
                setattr(s, k, v)
        player = cfg.get("player", {})
        profile = "\n".join(f"- {k}: {v}" for k, v in player.items() if v not in ("", None, []))
    return s, profile


# --- 過去の課題（成長の追跡） ----------------------------------------------
def load_history(out_dir: Path, n: int = 3) -> str:
    p = out_dir / "history.json"
    if not p.exists():
        return ""
    items = json.loads(p.read_text(encoding="utf-8"))[-n:]
    return "\n".join(
        f"- {it['date']} {it['video']}: " + " / ".join(it["focus_points"]) for it in items if it["focus_points"]
    )


def append_history(out_dir: Path, video: Path, focus: list[str]) -> None:
    p = out_dir / "history.json"
    items = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    items.append({"date": dt.date.today().isoformat(), "video": video.name, "focus_points": focus})
    p.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


# --- 1本を処理 --------------------------------------------------------------
def process(video: Path, s: Settings, profile: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    est = estimate(video, s)
    print(f"▶ {video.name}（{est['duration'] / 60:.1f}分, 約{est['frames']}枚, 見積もり 約${est['usd']:.2f}）")
    res = Coach(s).run(video, profile=profile, history=load_history(out_dir))
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = out_dir / f"{stamp}_{video.stem}.md"
    cost = res.input_tokens / 1e6 * s.price_in_per_mtok + res.output_tokens / 1e6 * s.price_out_per_mtok
    footer = (
        f"\n\n---\n動画: {video.name} / 解析フレーム: {res.frames}枚 / "
        f"トークン: 入力{res.input_tokens:,} 出力{res.output_tokens:,} / 費用目安 ${cost:.2f}\n"
    )
    out.write_text(res.report + footer, encoding="utf-8")
    append_history(out_dir, video, res.focus_points)
    print(f"✔ レポート: {out}")
    return out


# --- フォルダ監視 -----------------------------------------------------------
def watch(inbox: Path, s: Settings, profile: str, out_dir: Path, poll: int, move_done: bool) -> None:
    inbox.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "processed.json"
    done: dict = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    sizes: dict[str, int] = {}
    print(f"👀 {inbox.resolve()} を監視中（Ctrl+C で終了）")
    while True:
        for f in sorted(inbox.iterdir()):
            if f.suffix.lower() not in VIDEO_EXT or not f.is_file():
                continue
            key = f"{f.name}:{f.stat().st_size}"
            if key in done:
                continue
            # コピー/同期の途中でないか：前回とサイズが同じなら完了とみなす
            size = f.stat().st_size
            if sizes.get(f.name) != size:
                sizes[f.name] = size
                continue
            try:
                report = process(f, s, profile, out_dir)
                done[key] = str(report)
                if move_done:
                    (inbox / "done").mkdir(exist_ok=True)
                    shutil.move(str(f), inbox / "done" / f.name)
            except Exception as e:  # 1本失敗しても監視は続ける
                print(f"✖ {f.name}: {e}", file=sys.stderr)
                done[key] = f"error: {e}"
            state_path.write_text(json.dumps(done, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(poll)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="apex_coach", description="Apex の録画を Claude にコーチングしてもらう")
    ap.add_argument("--config", type=Path, default=Path("config.toml"))
    ap.add_argument("--out", type=Path, default=Path("reports"), help="レポートの保存先")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="録画を1本コーチング")
    a.add_argument("video", type=Path)
    a.add_argument("--start", type=float, help="分析開始（秒）")
    a.add_argument("--end", type=float, help="分析終了（秒）")
    a.add_argument("--interval", type=float, help="何秒ごとに1枚切り出すか")
    a.add_argument("--dry-run", action="store_true", help="費用の見積もりだけ表示")

    w = sub.add_parser("watch", help="フォルダを監視して新しい録画を自動コーチング")
    w.add_argument("inbox", type=Path)
    w.add_argument("--poll", type=int, default=30, help="確認間隔（秒）")
    w.add_argument("--move-done", action="store_true", help="処理済みの動画を inbox/done に移動")

    args = ap.parse_args(argv)
    s, profile = load_config(args.config)

    if args.cmd == "analyze":
        for k in ("start", "end", "interval"):
            if getattr(args, k) is not None:
                setattr(s, k, getattr(args, k))
        if args.dry_run:
            e = estimate(args.video, s)
            print(
                f"長さ {e['duration'] / 60:.1f}分 / {e['interval']:.1f}秒ごと {e['frames']}枚 / "
                f"見積もり 約${e['usd']:.2f}（モデル {s.model}）"
            )
            return
        process(args.video, s, profile, args.out)
    else:
        watch(args.inbox, s, profile, args.out, args.poll, args.move_done)
