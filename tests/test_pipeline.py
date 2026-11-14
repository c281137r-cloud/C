"""API キーなしで動くテスト。Claude の返事は偽物で代用し、それ以外は本物の処理を通す。

    python -m pytest tests -q
"""

import json
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from apex_coach import cli, coach  # noqa: E402
from apex_coach.frames import extract_frames, fmt_ts, plan_interval, probe_duration  # noqa: E402

REPORT = (
    "# 試合コーチングレポート\n## 総評\nok\n## 改善点（優先度順）\n- [01:02] x\n"
    "## 次回の重点課題\n1. 高所を取る\n2. 回復は遮蔽物の裏で\n3. 漁夫を警戒\n"
)


@pytest.fixture(scope="session")
def video(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("v") / "match.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=10",
         "-t", "95", "-c:v", "libx264", "-preset", "ultrafast", str(p)],
        check=True,
    )
    return p


class FakeClient:
    """client.beta.messages.stream(...) を真似る。"""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.beta = NS(messages=NS(stream=self._stream))

    def _stream(self, **kw):
        client = self

        class S:
            def __enter__(s):
                return s

            def __exit__(s, *a):
                pass

            def get_final_message(s):
                content = kw["messages"][0]["content"]
                imgs = sum(1 for b in content if b["type"] == "image")
                with client.lock:
                    client.calls.append((imgs, kw))
                text = "### 区間の流れ\n- [00:02] test" if imgs else REPORT
                return NS(stop_reason="end_turn", stop_details=None,
                          content=[NS(type="text", text=text)],
                          usage=NS(input_tokens=1000, output_tokens=100))

        return S()


@pytest.fixture
def fake(monkeypatch):
    f = FakeClient()
    monkeypatch.setattr(coach.anthropic, "Anthropic", lambda: f)
    return f


def test_helpers():
    assert fmt_ts(62) == "01:02" and fmt_ts(3725) == "1:02:05"
    assert plan_interval(900, 2, 400) == 2.25
    assert plan_interval(60, 2, 400) == 2


def test_frames(video, tmp_path):
    assert 94 < probe_duration(video) < 96
    frames = extract_frames(video, tmp_path, 2.0, 1280)
    assert len(frames) == 48 and frames[1].label == "00:02"
    part = extract_frames(video, tmp_path / "p", 2.0, 640, start=30, end=50)
    assert part[0].label == "00:30" and len(part) == 10


def test_config_example_parses():
    s, profile = cli.load_config(Path(__file__).resolve().parents[1] / "config.example.toml")
    assert s.model == "claude-opus-5-5" and "ランク" in profile and "感度設定" not in profile


def test_parse_focus_points():
    assert coach.parse_focus_points(REPORT) == ["高所を取る", "回復は遮蔽物の裏で", "漁夫を警戒"]
    assert coach.parse_focus_points("# no section") == []


def test_full_pipeline_and_history(video, tmp_path, fake):
    s = coach.Settings()
    cli.process(video, s, "- ランク: ゴールド", tmp_path)
    assert sorted(c[0] for c in fake.calls) == [0, 8, 40]  # 40枚+8枚の区間分析と統合1回
    req = fake.calls[-1][1]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert "ランク" in req["messages"][0]["content"][0]["text"]
    # 2 本目：前回の課題がプロンプトに入る
    fake.calls.clear()
    time.sleep(1.1)
    cli.process(video, s, "", tmp_path)
    syn = [c for c in fake.calls if c[0] == 0][0][1]["messages"][0]["content"][0]["text"]
    assert "高所を取る" in syn
    hist = json.loads((tmp_path / "history.json").read_text(encoding="utf-8"))
    assert len(hist) == 2 and len(list(tmp_path.glob("*.md"))) == 2


def test_watch_folder(video, tmp_path, fake):
    inbox, out = tmp_path / "inbox", tmp_path / "out"
    threading.Thread(target=cli.watch, args=(inbox, coach.Settings(), "", out, 1, True), daemon=True).start()
    time.sleep(1.5)
    shutil.copy(video, inbox / "m1.mp4")
    for _ in range(30):
        if (inbox / "done" / "m1.mp4").exists():
            break
        time.sleep(0.5)
    assert (inbox / "done" / "m1.mp4").exists()
    assert len(list(out.glob("*_m1.md"))) == 1
    assert "m1.mp4" in (out / "processed.json").read_text(encoding="utf-8")


def test_real_sdk_request_shape():
    """本物の SDK が送る HTTP リクエストの中身を確認（ローカルの偽サーバーで受ける）。"""
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            got["beta"] = self.headers.get("anthropic-beta")
            self.send_response(401)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"type":"error","error":{"type":"authentication_error","message":"x"}}')

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    c = anthropic.Anthropic(api_key="x", base_url=f"http://127.0.0.1:{srv.server_port}", max_retries=0)
    with pytest.raises(anthropic.AuthenticationError):
        coach.Coach(coach.Settings(), client=c)._call([{"type": "text", "text": "hi"}], 100)
    srv.shutdown()
    b = got["body"]
    assert b["model"] == "claude-opus-5-5" and b["fallbacks"] == "default"
    assert b["output_config"] == {"effort": "high"} and b["stream"] is True
    assert "server-side-fallback-2026-07-01" in got["beta"]
