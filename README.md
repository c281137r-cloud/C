# C

## 片道GO! 新着通知

トヨタレンタカーの区間限定特別料金「[片道GO!](https://cp.toyota.jp/rentacar/)」に
新しい車両が掲載されたら、スマホや Discord などに通知する仕組みです。

- GitHub Actions が **10 分ごと** に掲載ページをチェックします（サーバー不要・無料枠で動作）
- 前回なかった車両、または「受付終了」から受付中に戻った車両だけを通知します
- 出発エリア・返却エリア・車種などのキーワードで絞り込めます
- 既読状態は `state/katamichi_go.json` に保存され、Actions が自動でコミットします

通知はこんな感じです:

```
【片道GO!】新着 1 件
https://cp.toyota.jp/rentacar/

🚗 ハイエースバン 品川400わ1234  [関東→近畿]
出発: トヨタレンタリース東京 新宿店（東京都 新宿区）
返却: トヨタレンタリース大阪 新大阪店（大阪府 大阪市）
出発期間: 2026年11月6日 ～ 11月12日
条件: 禁煙 AT
予約☎: 03-0000-0000（新宿店）
```

> 片道GO! は Web 予約不可・電話予約のみ・早い者勝ちです。通知が来たらすぐ電話しましょう。

### セットアップ

1. このブランチを **デフォルトブランチ（main）にマージ** します。
   GitHub Actions の定期実行（schedule）はデフォルトブランチでしか動きません。
2. 下の「通知先の設定」から 1 つ以上を選び、リポジトリの
   **Settings → Secrets and variables → Actions → Secrets** に登録します。
3. **Actions → 片道GO! 新着チェック → Run workflow** で `test_notify` にチェックを入れて実行し、
   通知が届くか確認します。
4. あとは放っておけば 10 分ごとにチェックされます。初回の実行では、その時点の掲載は
   「既読」として記録するだけで通知しません（最初に大量に通知が来るのを防ぐため）。

### 通知先の設定（Secrets）

| 通知先 | 登録する Secret | メモ |
| --- | --- | --- |
| **ntfy**（いちばん手軽） | `NTFY_TOPIC` | スマホに [ntfy アプリ](https://ntfy.sh/) を入れ、推測されにくいトピック名（例: `katamichi-go-8f3k2x9q`）を購読。同じ名前を Secret に登録するだけ。アカウント不要。 |
| Discord | `DISCORD_WEBHOOK_URL` | チャンネル設定 → 連携サービス → ウェブフック で URL を作成 |
| Slack | `SLACK_WEBHOOK_URL` | Incoming Webhook の URL |
| LINE | `LINE_CHANNEL_ACCESS_TOKEN` と `LINE_TO` | LINE Developers で Messaging API チャネルを作成し、チャネルアクセストークンと自分のユーザー ID を登録。友だち追加も必要。（LINE Notify は 2025 年 3 月に終了しています） |

複数設定した場合はすべてに送られます。ntfy のトピックは名前を知っていれば誰でも読めるので、
推測されにくい名前にしてください。自前の ntfy サーバーを使う場合は Variables に `NTFY_SERVER` を設定します。

### 絞り込みの設定（Variables・任意）

**Settings → Secrets and variables → Actions → Variables** に登録します。
複数指定はカンマ（`,` または `、`）区切りです。未設定なら全件通知します。

| Variable | 例 | 意味 |
| --- | --- | --- |
| `FILTER_START_AREAS` | `関東,近畿` | 出発エリアがこのどれか（北海道・東北・関東・中部・近畿・九州） |
| `FILTER_RETURN_AREAS` | `九州` | 返却エリアがこのどれか |
| `FILTER_KEYWORDS` | `東京,ハイエース` | 店舗名・車種・条件などにどれかを含む |
| `FILTER_EXCLUDE_KEYWORDS` | `プロボックス` | どれかを含むものは除外 |
| `NOTIFY_ON_FIRST_RUN` | `1` | 初回実行でも現在の受付中車両を通知する |

### 手元で動かす

Python 3.9 以上・標準ライブラリのみで動きます。

```sh
python katamichi_go/watcher.py --list              # 現在の掲載一覧を表示
python katamichi_go/watcher.py --dry-run           # 新着判定だけ（通知・保存しない）
NTFY_TOPIC=your-topic python katamichi_go/watcher.py --test-notify   # 通知テスト
python -m unittest discover -s katamichi_go/tests  # テスト
```

### 注意

- GitHub Actions の定期実行は混雑時に遅れることがあり、ぴったり 10 分おきにはなりません。
  また、リポジトリに長期間動きがないと GitHub が定期実行を自動停止することがあります。その場合は Actions タブから再度有効化してください。
- 掲載ページの HTML 構成が変わると取得できなくなります。その場合は 1 件も取れなかった時点で
  ワークフローが失敗し、GitHub から失敗通知メールが届きます。
- アクセスは 10 分に 1 回のページ取得のみで、サイトに負荷をかけないようにしています。
