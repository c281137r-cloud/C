# Apex 録画 自動コーチング

PS5 で録画した Apex Legends の試合動画を Claude に見せて、コーチングレポート（Markdown）を自動で作るツールです。

```
PS5で録画 → PC のフォルダに置く → 自動で解析 → reports/ にレポートが出る
```

## 仕組み

1. **ffmpeg** で録画から 2 秒ごとに静止画を切り出す（HUD の文字が読めるよう横 1280px）
2. 40 枚ずつ区間に分けて **Claude API**（既定は `claude-opus-5-5`）に渡し、区間ごとに「何が起きたか・良かった点・改善点」をタイムスタンプ付きで分析
3. 区間メモを統合して最終レポートを作成
4. レポート末尾の「次回の重点課題」を `reports/history.json` に記録し、**次の録画のときに「前回の課題が改善したか」も判定**

レポートの構成：総評 / ハイライト（重要シーン） / 良かった点 / 改善点（優先度順・練習方法つき） / 前回からの変化 / 次回の重点課題3つ

> **できないこと**：Claude は動画をそのまま見るのではなく、数秒おきの静止画を見ています。立ち回り・判断・回復やリロードのタイミング・リング移動・味方との距離などは評価できますが、**エイムの追いエイムやリコイル制御の細かさは判断できません**（レポートにもそう書かれます）。音声（足音・味方のVC）も使いません。

## セットアップ（PC で 1 回だけ）

必要なもの：Python 3.11 以上、ffmpeg、Anthropic の API キー（[console.anthropic.com](https://console.anthropic.com) で発行。従量課金）

```bash
# ffmpeg のインストール
#   Windows: winget install ffmpeg
#   Mac:     brew install ffmpeg

pip install -r requirements.txt
cp config.example.toml config.toml      # 自分のランクや使用レジェンド、悩みを書く

# API キー（Windows PowerShell なら  $env:ANTHROPIC_API_KEY="sk-ant-..." ）
export ANTHROPIC_API_KEY=sk-ant-...
```

## 使い方

```bash
# 1本だけコーチング
python -m apex_coach analyze 録画.mp4

# 費用の見積もりだけ（API は呼ばない）
python -m apex_coach analyze 録画.mp4 --dry-run

# 一部だけ（例：5分〜12分の交戦部分）
python -m apex_coach analyze 録画.mp4 --start 300 --end 720

# フォルダ監視：inbox に動画を置くたびに自動でレポートを作る
python -m apex_coach watch inbox --move-done
```

`watch` は 30 秒おきにフォルダを見て、コピー途中のファイルはサイズが落ち着くまで待ってから処理します。処理済みの動画は `processed.json` に記録されるので二重に解析されません（`--move-done` を付けると `inbox/done/` に移動）。

## PS5 から PC に動画を送る方法

**PS5 側の設定（おすすめ）**
- 設定 → キャプチャーとブロードキャスト → キャプチャー → **ビデオクリップのファイル形式を MP4** に、**解像度は 1080p 以上**（HUD の文字を読むため）
- 1 試合まるごと録るなら、試合開始時に クリエイトボタン → **「新しい録画を開始」**。終わった直後なら「直近のゲームプレイを保存」でも OK

**方法A：USB メモリ（確実・長い動画もOK）**
メディアギャラリー → 動画を選ぶ → 「USBドライブにコピー」→ PC の `inbox` フォルダに入れる。`watch` を起動しておけば置いた瞬間から解析が始まります。

**方法B：ほぼ全自動（スマホ経由）**
1. PS5：キャプチャー設定で **自動アップロード** をオン → PS App（スマホ）に動画が届く（クラウドには一定期間のみ保存。長いクリップは対象外のことがあります）
2. スマホ：PS App から端末に保存 → Google ドライブ / OneDrive などの特定フォルダへ自動バックアップ
3. PC：そのフォルダを同期フォルダにして `python -m apex_coach watch <同期フォルダ>` を常駐させる

## 費用の目安（15 分の試合 1 本）

| 設定 | 枚数 | 目安 |
|---|---|---|
| 既定（Opus 5.5・2秒ごと・1280px） | 約400枚 | 約 $3 |
| 節約（`interval = 4`, `width = 960`） | 約230枚 | 約 $1.3 |
| さらに節約（上記＋`model = "claude-sonnet-5-5"`, 単価 2/10） | 約230枚 | 約 $0.6 |

見積もりは上限寄りです。実際の費用はレポート末尾に表示されます。交戦シーンだけ `--start/--end` で切り出すのが一番安くて効果的です。

## ファイル構成

```
apex_coach/
  frames.py   ffmpeg で静止画を切り出す
  prompts.py  コーチの指示文（見るポイントやレポートの形を変えたいときはここ）
  coach.py    Claude API 呼び出し・区間分析・統合
  cli.py      analyze / watch コマンド、履歴管理
config.example.toml  プロフィールと解析設定のひな形
```
