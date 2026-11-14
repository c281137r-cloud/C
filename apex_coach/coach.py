"""Claude API に静止画を渡してコーチングレポートを作る。"""

from __future__ import annotations

import base64
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

import anthropic

from . import prompts
from .frames import Frame, chunk, extract_frames, fmt_ts, plan_interval, probe_duration

# 画像1枚あたりのおおよその入力トークン数（幅 x 高さ / 750）
def image_tokens(width: int) -> int:
    height = width * 9 // 16
    return width * height // 750


@dataclass
class Settings:
    model: str = "claude-opus-5-5"
    effort: str = "high"
    interval: float = 2.0  # 何秒ごとに1枚切り出すか
    width: int = 1280  # 切り出す画像の横幅（HUDの文字を読むため 1280 推奨）
    max_frames: int = 400  # 長い動画でもこれ以上は切り出さない（費用の上限）
    chunk_size: int = 40  # 1回のAPI呼び出しに渡す枚数
    parallel: int = 3
    start: float = 0.0
    end: float | None = None
    price_in_per_mtok: float = 4.0  # Claude Opus 5.5 の入力単価（USD / 100万トークン）
    price_out_per_mtok: float = 20.0


@dataclass
class Result:
    report: str
    focus_points: list[str] = field(default_factory=list)
    frames: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def estimate(video: Path, s: Settings) -> dict:
    duration = probe_duration(video)
    end = s.end if s.end is not None else duration
    span = max(0.0, end - s.start)
    interval = plan_interval(span, s.interval, s.max_frames)
    n = int(span // interval) + 1
    in_tok = n * (image_tokens(s.width) + 15) + 3000 * (n // s.chunk_size + 2)
    out_tok = 4000 * (n // s.chunk_size + 2)
    usd = in_tok / 1e6 * s.price_in_per_mtok + out_tok / 1e6 * s.price_out_per_mtok
    return {"duration": duration, "interval": interval, "frames": n, "usd": usd}


class Coach:
    def __init__(self, settings: Settings, client: anthropic.Anthropic | None = None):
        self.s = settings
        self.client = client or anthropic.Anthropic()
        self.in_tok = 0
        self.out_tok = 0

    # --- API 呼び出し -------------------------------------------------------
    def _call(self, content: list[dict], max_tokens: int) -> str:
        with self.client.beta.messages.stream(
            model=self.s.model,
            max_tokens=max_tokens,
            system=prompts.SYSTEM,
            output_config={"effort": self.s.effort},
            # 安全分類器に誤って拒否された場合、サーバー側で別モデルに自動で切り替える
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": content}],
        ) as stream:
            msg = stream.get_final_message()
        self.in_tok += msg.usage.input_tokens
        self.out_tok += msg.usage.output_tokens
        if msg.stop_reason == "refusal":
            detail = msg.stop_details.explanation if msg.stop_details else ""
            raise RuntimeError(f"Claude が分析を拒否しました: {detail}")
        text = "".join(b.text for b in msg.content if b.type == "text")
        if msg.stop_reason == "max_tokens":
            text += "\n\n（出力上限に達したため途中で切れています）"
        return text

    # --- 区間ごとの分析 -----------------------------------------------------
    def _analyze_chunk(self, frames: list[Frame], idx: int, total: int) -> str:
        content: list[dict] = [
            {
                "type": "text",
                "text": prompts.CHUNK_INSTRUCTION.format(
                    idx=idx, total=total, start=frames[0].label, end=frames[-1].label
                ),
            }
        ]
        for f in frames:
            content.append({"type": "text", "text": f"[{f.label}]"})
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/jpeg",
                        "data": base64.standard_b64encode(f.path.read_bytes()).decode(),
                    },
                }
            )
        return self._call(content, max_tokens=16000)

    # --- 全体 ---------------------------------------------------------------
    def run(self, video: Path, profile: str = "", history: str = "", log=print) -> Result:
        duration = probe_duration(video)
        end = self.s.end if self.s.end is not None else duration
        interval = plan_interval(end - self.s.start, self.s.interval, self.s.max_frames)
        with tempfile.TemporaryDirectory(prefix="apex_frames_") as tmp:
            log(f"フレーム抽出中…（{interval:.1f}秒ごと）")
            frames = extract_frames(video, Path(tmp), interval, self.s.width, self.s.start, self.s.end)
            if not frames:
                raise RuntimeError("フレームを切り出せませんでした。動画ファイルを確認してください。")
            groups = chunk(frames, self.s.chunk_size)
            log(f"{len(frames)} 枚を {len(groups)} 区間に分けて分析中…")
            with ThreadPoolExecutor(max_workers=self.s.parallel) as pool:
                futures = [
                    pool.submit(self._analyze_chunk, g, i + 1, len(groups)) for i, g in enumerate(groups)
                ]
                notes = [fu.result() for fu in futures]

        log("レポートを作成中…")
        joined = "\n\n".join(f"<区間{i + 1}>\n{n}\n</区間{i + 1}>" for i, n in enumerate(notes))
        report = self._call(
            [
                {
                    "type": "text",
                    "text": prompts.SYNTHESIS_INSTRUCTION.format(
                        duration=fmt_ts(duration),
                        profile=f"## 選手プロフィール\n{profile}\n" if profile else "",
                        history=f"## 過去の録画で指摘した課題\n{history}\n" if history else "",
                        notes=joined,
                    ),
                }
            ],
            max_tokens=32000,
        )
        return Result(
            report=report,
            focus_points=parse_focus_points(report),
            frames=len(frames),
            input_tokens=self.in_tok,
            output_tokens=self.out_tok,
        )


def parse_focus_points(report: str) -> list[str]:
    m = re.search(r"^##\s*次回の重点課題\s*$(.*?)(?=^#|\Z)", report, re.M | re.S)
    if not m:
        return []
    return [re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", ln).strip() for ln in m.group(1).splitlines() if re.match(r"^\s*(?:[-*]|\d+[.)])\s+", ln)][:3]
