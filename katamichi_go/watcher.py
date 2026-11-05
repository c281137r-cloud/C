#!/usr/bin/env python3
"""トヨタレンタカー「片道GO!」の新着監視スクリプト.

https://cp.toyota.jp/rentacar/ の掲載一覧を取得し、前回実行時に無かった
（または「受付終了」から受付中に戻った）車両があれば通知する。

標準ライブラリのみで動作する。設定はすべて環境変数で行う（README 参照）。
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path

PAGE_URL = "https://cp.toyota.jp/rentacar/"
USER_AGENT = "Mozilla/5.0 (compatible; katamichi-go-watcher/1.0)"
JST = timezone(timedelta(hours=9))

AREAS = {
    "1": "北海道",
    "2": "東北",
    "3": "関東",
    "4": "中部",
    "5": "近畿",
    "6": "中国",
    "7": "四国",
    "8": "九州",
}

# service-item 内で値を拾う要素のクラス名 -> フィールド名
FIELD_CLASSES = {
    "service-item__shop-start": "start_shop",
    "service-item__shop-return": "return_shop",
    "service-item__date": "period",
    "service-item__info__car-type": "car",
    "service-item__info__condition": "conditions",
    "service-item__reserve-shop": "reserve_shop",
    "service-item__reserve-tel": "tel",
}

VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "source", "track", "wbr",
}


@dataclass
class Listing:
    start_shop: str = ""
    return_shop: str = ""
    period: str = ""
    car: str = ""
    conditions: str = ""
    reserve_shop: str = ""
    tel: str = ""
    start_area: str = ""
    return_area: str = ""
    closed: bool = False  # True = 「受付終了」

    @property
    def key(self) -> str:
        # 車種欄にナンバーが入っているため、これでほぼ一意になる
        return " | ".join([self.start_shop, self.return_shop, self.car, self.period])

    @property
    def start_area_name(self) -> str:
        return AREAS.get(self.start_area, self.start_area)

    @property
    def return_area_name(self) -> str:
        return AREAS.get(self.return_area, self.return_area)

    def searchable_text(self) -> str:
        return " ".join([
            self.start_shop, self.return_shop, self.period, self.car,
            self.conditions, self.reserve_shop, self.start_area_name,
            self.return_area_name,
        ])


class _ListingParser(HTMLParser):
    """<li class="service-item"> を Listing に変換する寛容なパーサ."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.listings: list[Listing] = []
        self._stack: list[tuple[str, set[str]]] = []
        self._current: Listing | None = None
        self._item_depth = -1
        self._texts: dict[str, list[str]] = {}

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        classes = set((attrs_d.get("class") or "").split())
        if tag in VOID_TAGS:
            if tag == "br" and self._current is not None:
                self._add_text(" ")
            return
        if tag == "li" and "service-item" in classes:
            self._current = Listing(
                start_area=attrs_d.get("data-start-area") or "",
                return_area=attrs_d.get("data-return-area") or "",
            )
            self._texts = {}
            self._item_depth = len(self._stack)
        if self._current is not None and "service-item__body" in classes:
            self._current.closed = "show-entry-end" in classes
        self._stack.append((tag, classes))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS and self._stack and self._stack[-1][0] == tag:
            self._stack.pop()

    def handle_endtag(self, tag):
        # 対応する開始タグがスタックに無い閉じタグ（壊れた HTML）は無視する
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                del self._stack[i:]
                break
        else:
            return
        if self._current is not None and len(self._stack) <= self._item_depth:
            self._finish_item()

    def handle_data(self, data):
        if self._current is not None:
            self._add_text(data)

    def _add_text(self, data: str) -> None:
        field = None
        for _tag, classes in reversed(self._stack):
            if "label-sp" in classes or "service-item__group-btn" in classes:
                return  # ラベル（「出発店舗」など）やボタン文言は除外
            for cls in classes:
                if cls in FIELD_CLASSES:
                    field = FIELD_CLASSES[cls]
                    break
            if field:
                break
        if field:
            self._texts.setdefault(field, []).append(data)

    def _finish_item(self) -> None:
        item = self._current
        assert item is not None
        for field, parts in self._texts.items():
            setattr(item, field, _clean("".join(parts)))
        self.listings.append(item)
        self._current = None
        self._item_depth = -1
        self._texts = {}

    def close(self):
        super().close()
        if self._current is not None:
            self._finish_item()


def _clean(text: str) -> str:
    text = html.unescape(text).replace("　", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text.replace(" （", "（")


def parse_listings(page_html: str) -> list[Listing]:
    parser = _ListingParser()
    parser.feed(page_html)
    parser.close()
    # 「出発」タブと「到着」タブで同じ車両が二重に掲載されているので重複排除
    unique: dict[str, Listing] = {}
    for item in parser.listings:
        if item.car or item.start_shop:
            unique.setdefault(item.key, item)
    return list(unique.values())


def fetch_page(url: str = PAGE_URL, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Language": "ja,en;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


# --------------------------------------------------------------------------
# フィルタ
# --------------------------------------------------------------------------

def _split_env(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [s.strip() for s in re.split(r"[,、\n]", raw) if s.strip()]


@dataclass
class Filters:
    start_areas: list[str]
    return_areas: list[str]
    keywords: list[str]
    exclude_keywords: list[str]

    @classmethod
    def from_env(cls) -> "Filters":
        return cls(
            start_areas=_split_env("FILTER_START_AREAS"),
            return_areas=_split_env("FILTER_RETURN_AREAS"),
            keywords=_split_env("FILTER_KEYWORDS"),
            exclude_keywords=_split_env("FILTER_EXCLUDE_KEYWORDS"),
        )

    def match(self, item: Listing) -> bool:
        if self.start_areas and item.start_area_name not in self.start_areas:
            return False
        if self.return_areas and item.return_area_name not in self.return_areas:
            return False
        text = item.searchable_text()
        if self.keywords and not any(k in text for k in self.keywords):
            return False
        if any(k in text for k in self.exclude_keywords):
            return False
        return True


# --------------------------------------------------------------------------
# 状態管理
# --------------------------------------------------------------------------

def load_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def diff_listings(listings: list[Listing], state: dict | None) -> tuple[list[Listing], dict]:
    """通知すべき車両と、新しい状態を返す.

    通知対象: 受付中で、かつ「前回いなかった」または「前回は受付終了だった」もの。
    ページから消えた車両は状態から削除する。
    """
    prev = (state or {}).get("listings", {})
    now = datetime.now(JST).isoformat(timespec="seconds")
    new_items: list[Listing] = []
    new_state_listings: dict[str, dict] = {}
    for item in listings:
        before = prev.get(item.key)
        if not item.closed and (before is None or before.get("closed")):
            new_items.append(item)
        new_state_listings[item.key] = {
            "closed": item.closed,
            "first_seen": (before or {}).get("first_seen", now),
        }
    return new_items, {"listings": new_state_listings}


# --------------------------------------------------------------------------
# 通知
# --------------------------------------------------------------------------

def format_listing(item: Listing) -> str:
    lines = [
        f"🚗 {item.car}  [{item.start_area_name}→{item.return_area_name}]",
        f"出発: {item.start_shop}",
        f"返却: {item.return_shop}",
        f"出発期間: {item.period}",
    ]
    if item.conditions:
        lines.append(f"条件: {item.conditions}")
    lines.append(f"予約☎: {item.tel}（{item.reserve_shop}）")
    return "\n".join(lines)


def build_messages(items: list[Listing], limit: int) -> list[str]:
    """件数見出し付きで、各通知先の文字数上限に収まるようにまとめる."""
    header = f"【片道GO!】新着 {len(items)} 件\n{PAGE_URL}"
    messages: list[str] = []
    current = header
    for item in items:
        block = format_listing(item)
        if len(current) + 2 + len(block) > limit:
            messages.append(current)
            current = block
        else:
            current += "\n\n" + block
    messages.append(current)
    return messages


def _post_json(url: str, payload: dict, headers: dict | None = None) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        **(headers or {}),
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        resp.read()


def notify_discord(items: list[Listing], webhook: str) -> None:
    for msg in build_messages(items, 1900):
        _post_json(webhook, {"content": msg})


def notify_slack(items: list[Listing], webhook: str) -> None:
    for msg in build_messages(items, 3500):
        _post_json(webhook, {"text": msg})


def notify_ntfy(items: list[Listing], topic: str, server: str) -> None:
    url = server.rstrip("/") + "/" + urllib.parse.quote(topic)
    for msg in build_messages(items, 3500):
        req = urllib.request.Request(url, data=msg.encode("utf-8"), method="POST", headers={
            # ヘッダは ASCII のみ可。日本語タイトルは RFC 2047 形式で渡す
            "Title": "=?UTF-8?B?" + _b64("片道GO! 新着") + "?=",
            "Click": PAGE_URL,
            "Tags": "red_car",
            "User-Agent": USER_AGENT,
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()


def notify_line(items: list[Listing], token: str, to: str) -> None:
    # LINE Messaging API の push（1 回のリクエストで最大 5 メッセージ）
    msgs = build_messages(items, 4900)
    for i in range(0, len(msgs), 5):
        _post_json(
            "https://api.line.me/v2/bot/message/push",
            {"to": to, "messages": [{"type": "text", "text": m} for m in msgs[i:i + 5]]},
            headers={"Authorization": f"Bearer {token}"},
        )


def _b64(s: str) -> str:
    return base64.b64encode(s.encode("utf-8")).decode("ascii")


def send_notifications(items: list[Listing]) -> list[str]:
    """設定済みの全通知先へ送る。送れた通知先の名前を返す."""
    sent: list[str] = []
    errors: list[str] = []
    targets = []
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        targets.append(("Discord", lambda: notify_discord(items, os.environ["DISCORD_WEBHOOK_URL"])))
    if os.environ.get("SLACK_WEBHOOK_URL"):
        targets.append(("Slack", lambda: notify_slack(items, os.environ["SLACK_WEBHOOK_URL"])))
    if os.environ.get("NTFY_TOPIC"):
        server = os.environ.get("NTFY_SERVER") or "https://ntfy.sh"
        targets.append(("ntfy", lambda: notify_ntfy(items, os.environ["NTFY_TOPIC"], server)))
    if os.environ.get("LINE_CHANNEL_ACCESS_TOKEN") and os.environ.get("LINE_TO"):
        targets.append(("LINE", lambda: notify_line(
            items, os.environ["LINE_CHANNEL_ACCESS_TOKEN"], os.environ["LINE_TO"])))
    if not targets:
        print("⚠ 通知先が 1 つも設定されていません（README の「通知先の設定」を参照）")
    for name, fn in targets:
        try:
            fn()
            sent.append(name)
        except (urllib.error.URLError, OSError) as e:
            detail = ""
            if isinstance(e, urllib.error.HTTPError):
                detail = e.read().decode("utf-8", errors="replace")[:300]
            errors.append(f"{name}: {e} {detail}")
    for err in errors:
        print(f"✖ 通知失敗 {err}", file=sys.stderr)
    if errors and not sent:
        raise RuntimeError("すべての通知先への送信に失敗しました")
    return sent


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="片道GO! の新着を通知する")
    ap.add_argument("--state", default=os.environ.get("STATE_FILE", "state/katamichi_go.json"),
                    help="既読状態を保存する JSON ファイル")
    ap.add_argument("--html", help="ページを取得せずにこのローカル HTML を使う（テスト用）")
    ap.add_argument("--dry-run", action="store_true", help="通知も状態保存もしない")
    ap.add_argument("--list", action="store_true", help="現在の掲載一覧を表示して終了")
    ap.add_argument("--test-notify", action="store_true",
                    help="受付中の先頭 1 件を使って通知先の疎通テストをする")
    args = ap.parse_args(argv)

    page = Path(args.html).read_text(encoding="utf-8") if args.html else fetch_page()
    listings = parse_listings(page)
    if not listings:
        # ページ構成の変更などで 1 件も取れない場合は、状態を壊さずに失敗させる
        print("✖ 掲載が 1 件も見つかりません。ページ構成が変わった可能性があります。", file=sys.stderr)
        return 2

    open_items = [x for x in listings if not x.closed]
    print(f"掲載 {len(listings)} 件（受付中 {len(open_items)} 件 / 受付終了 {len(listings) - len(open_items)} 件）")

    if args.list:
        for item in listings:
            mark = "×受付終了" if item.closed else "○受付中"
            print(f"\n[{mark}]\n{format_listing(item)}")
        return 0

    filters = Filters.from_env()

    if args.test_notify:
        sample = [x for x in open_items if filters.match(x)][:1] or open_items[:1]
        print(f"テスト通知を送信: {[x.key for x in sample]}")
        print("送信先:", send_notifications(sample))
        return 0

    state_path = Path(args.state)
    state = load_state(state_path)
    new_items, new_state = diff_listings(listings, state)
    matched = [x for x in new_items if filters.match(x)]

    if state is None and os.environ.get("NOTIFY_ON_FIRST_RUN", "") not in ("1", "true", "yes"):
        print(f"初回実行のため、現在の {len(new_items)} 件は既読として記録のみ行います。")
        matched = []

    print(f"新着 {len(new_items)} 件 / うちフィルタ一致 {len(matched)} 件")
    for item in matched:
        print("  +", item.key)

    if args.dry_run:
        return 0

    if matched:
        sent = send_notifications(matched)
        print("通知送信先:", ", ".join(sent) or "なし")
    save_state(state_path, new_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
