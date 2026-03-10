# TradingView Snapshot + Ollama 分析環境

ZeroClawを使わず、**Python + Playwright + Ollama (qwen3.5:4b)** だけで動くシンプル構成。
証券会社の約定照会CSVから銘柄を読み取り、TradingViewの1分足チャートを自動撮影・マーカー合成・AI分析する。

---

## 構成

```
tv-snapshot-app/
├── .devcontainer/
│   ├── devcontainer.json      # VS Code DevContainer設定
│   ├── docker-compose.yml     # ollama + devcontainer（GPU統合・1ファイルに統合済み）
│   └── Dockerfile             # Python 3.11-slim-bookworm + Playwright環境
├── pyproject.toml             # Pythonパッケージ（uv管理）
├── csv/                       # 約定照会CSVの置き場
├── scripts/
│   ├── batch_snapshot.py      # メイン: 撮影→マーカー合成→Ollama分析
│   └── export_prompt.py       # 外部AIエージェント用プロンプト・データ出力
├── tests/
│   └── test_batch_snapshot.py # ユニットテスト
└── snapshots/                 # 撮影画像・分析テキストの保存先
```

---

## セットアップ

### 1. フォルダ作成（PowerShell）

```powershell
New-Item -ItemType Directory -Path "$env:USERPROFILE\Desktop\tv-snapshot-app"
cd "$env:USERPROFILE\Desktop\tv-snapshot-app"
New-Item -ItemType Directory -Path ".devcontainer", "scripts", "snapshots", "csv"
```

### 2. VS CodeでDevContainerを開く

1. VS Codeでフォルダを開く
2. `Ctrl+Shift+P` → `Dev Containers: Reopen in Container`

初回ビルドは数分かかります。

### 3. Ollamaモデルのインストール

初回のみ手動でpull：

```powershell
docker exec ollama ollama pull qwen3.5:4b
```

インストール済みモデルの確認：

```powershell
docker exec ollama ollama list
```

> ⚠️ `docker` コマンドはコンテナ外（PowerShell）から実行してください。コンテナ内からは使えません。

---

## 使用モデル

| モデル | サイズ | 備考 |
|--------|--------|------|
| `qwen3.5:4b` | 3.4 GB | **現在の推奨モデル**。日本語安定・RTX 3060 Laptop (6GB VRAM) で動作確認済み |

> **補足**: `qwen3.5:4b` はデフォルトで思考（thinking）モードを持つが、プロンプト末尾に `/no_think` を付加することで抑制済み。

---

## CSVフォーマット

証券会社の約定照会CSVをそのまま使用できます。カラム名の揺れは自動吸収されます。

| 内部名 | 対応カラム名の例 | 備考 |
|--------|----------------|------|
| symbol | コード, 銘柄コード | 4〜5桁の銘柄コード（例: 5016） |
| date   | 約定日 | 時刻込み（`2026/03/06 09:03:40`）でも自動で日付部分のみ抽出 |
| price  | 約定単価(円), 約定単価 | カンマ区切り（`3,969.0`）も自動除去 |
| side   | 取引 | "信用新規" / "信用返済" |
| buysell | 売買 | "買建" / "売埋" / "売建" / "買埋" |
| qty    | 約定数量(株/口), 約定数量 | 任意 |
| time   | 約定時刻, 約定時間 | 任意 |

---

## 使い方

### batch_snapshot.py — 撮影・マーカー合成・Ollama分析

```bash
# 通常: 撮影 → マーカー合成 → Ollama分析 を一気通貫
uv run python3 /workspace/scripts/batch_snapshot.py --csv /workspace/csv/20260306_約定照会.csv

# 撮影のみ（分析スキップ）
uv run python3 /workspace/scripts/batch_snapshot.py --csv /workspace/csv/20260306_約定照会.csv --no-analysis

# 分析のみ（既存画像を再分析）
uv run python3 /workspace/scripts/batch_snapshot.py --csv /workspace/csv/20260306_約定照会.csv --analysis-only

# 空・エラーの銘柄だけ再分析（タイムアウト後の再実行に便利）
uv run python3 /workspace/scripts/batch_snapshot.py --csv /workspace/csv/20260306_約定照会.csv --retry-empty
```

オプション一覧：

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--csv` | 必須 | 約定照会CSVのパス |
| `--no-analysis` | - | Ollama分析をスキップ |
| `--analysis-only` | - | 撮影をスキップして分析のみ実行 |
| `--retry-empty` | - | 空・`[ERROR]` の分析ファイルのみ再分析。正常済みはスキップ |
| `--ollama-host` | `http://ollama:11434` | OllamaのURL |
| `--analysis-model` | `qwen3.5:4b` | 使用モデル |
| `--analysis-timeout` | `300` | タイムアウト秒数 |

---

### export_prompt.py — 外部AIエージェント用の出力

Claude / GPT-4o など外部のエージェントに分析させる場合に使う。
API呼び出しは行わず、プロンプトとデータをファイルに書き出すだけ。

```bash
uv run python3 /workspace/scripts/export_prompt.py --csv /workspace/csv/20260306_約定照会.csv
```

**Claudeのチャット（claude.ai）に貼り付ける場合：**
1. `TSE_5016_1m_20260306.png` をアップロード
2. `_prompt.txt` の `=== USER PROMPT ===` 以降をコピペ

（システムプロンプトはチャットUIでは不要）

---

## 出力ファイル

```
snapshots/
└── 20260306/
    ├── TSE_5016_1m_20260306_raw.png          ← 生スクリーンショット（削除しないこと）
    ├── TSE_5016_1m_20260306.png              ← マーカー合成済み（AI分析・外部共有に使う）
    ├── TSE_5016_1m_20260306_analysis.txt     ← Ollama分析結果
    ├── TSE_5016_1m_20260306_prompt.txt       ← 外部エージェント用プロンプト全文
    └── TSE_5016_1m_20260306_payload.json     ← 構造化データ（API連携用）
```

`_raw.png` は `--analysis-only` / `--retry-empty` 再実行時にマーカーを再合成するために使います。削除しないでください。

---

## チャートのダークモード

TradingViewのURLに `&theme=dark` を付加しています（デフォルト）。
ライトモードに戻す場合は `batch_snapshot.py` 冒頭の定数を編集してください：

```python
TV_URL_TEMPLATE = (
    "https://www.tradingview.com/chart/?symbol={symbol}"
    "&interval=1"
    # "&theme=dark"  ← コメントアウトでライトモードに戻す
)
```

---

## マーカー位置のズレ調整

チャートの価格レンジはCSVの約定価格から推定しています。
ローソク足とマーカーがずれる場合は `PRICE_PADDING_RATIO` を調整してください：

```python
# batch_snapshot.py 冒頭の定数
PRICE_PADDING_RATIO = 0.20  # 大きくするほどマーカーが中央寄りになる
```

---

## 損益計算ロジック

`estimate_pnl()` はロング・ショートを別スタックでFIFO管理します。

| 取引種別 | 計算式 |
|---------|--------|
| ロング（買建→売埋） | 売埋価格 − 買建価格 |
| ショート（売建→買埋） | 売建価格 − 買埋価格 |

未決済建玉がある場合は銘柄・数量・建値平均も出力されます。

---

## テスト

`batch_snapshot.py` のユニットテストが `tests/` に用意されています。
Ollamaや実際のTradingViewには接続せず、オフラインで高速に実行できます。

```bash
# 全テスト実行
uv run pytest tests/ -v

# 特定のクラスだけ実行
uv run pytest tests/test_batch_snapshot.py::TestEstimatePnl -v
```

### テスト構成（28件）

| クラス | 件数 | テスト対象 |
|---|---|---|
| `TestLoadTradesFromCsv` | 6 | CSV読み込み・カラムマッピング・バリデーション |
| `TestEstimatePnl` | 8 | FIFO損益計算・未決済建玉・複数回転 |
| `TestBuildTradeTable` | 5 | 約定テーブル生成・✅❌マーク |
| `TestPriceToY` | 4 | 価格→Y座標変換 |
| `TestDrawMarkers` | 4 | マーカー画像合成・エッジケース |

---

## トラブルシューティング

**DockerfileのビルドがPlaywrightエラーで失敗する**
ベースイメージが `python:3.11-slim`（Debian trixie）だとフォントパッケージが見つからずエラーになります。
`Dockerfile` の1行目が以下になっているか確認してください：
```dockerfile
FROM python:3.11-slim-bookworm
```

**CSVを読み込んで「0 銘柄×日付を処理」と表示される**
約定単価にカンマ（`3,969.0`）が含まれていて数値変換に失敗している可能性があります。
また約定日カラムに時刻（`2026/03/06 09:03:40`）が含まれていても自動で日付部分のみ抽出します。
どちらも現行スクリプトで対応済みです。

**Ollamaに繋がらない（404エラー）**
モデルがまだダウンロードされていない可能性があります：
```powershell
docker exec ollama ollama list                          # モデル一覧確認
docker exec ollama ollama pull qwen3.5:4b              # 手動pull
```

**AI分析が空白になる（1銘柄目）**
モデルの初回ロードに時間がかかり、タイムアウトすることがあります。
`--retry-empty` オプションで空の銘柄だけ再分析できます：
```bash
uv run python3 /workspace/scripts/batch_snapshot.py \
  --csv /workspace/csv/20260306_約定照会.csv --retry-empty
```

**AI分析がタイムアウトする**
デフォルトのタイムアウトは300秒です。それでも不足する場合は `--analysis-timeout` で延長できます：
```bash
uv run python3 /workspace/scripts/batch_snapshot.py \
  --csv /workspace/csv/20260306_約定照会.csv --analysis-only --analysis-timeout 600
```

**損益が0円と表示される**
`side`（取引）カラムと `buysell`（売買）カラムの両方が正しく読み込まれているか確認してください。
`batch_snapshot.py` は両カラムを結合して買建/売埋/売建/買埋を判定します。