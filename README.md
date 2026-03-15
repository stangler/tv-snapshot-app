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
├── scripts/
│   ├── __init__.py            # パッケージ化用（snapコマンド登録に必要）
│   ├── batch_snapshot.py      # メイン: 撮影→マーカー合成→プロンプト出力→Ollama分析
│   └── export_prompt.py       # 外部AIエージェント用プロンプト・データ出力（単体利用可）
├── csv/                       # 約定照会CSVの置き場
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

### 3. snapコマンドのインストール

```bash
uv sync
```

### 4. Ollamaモデルのインストール

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

### snap コマンド（推奨）

`uv sync` 後は `snap` コマンドで短く実行できます。

```bash
# 撮影 + プロンプト出力（デフォルト）
snap --date 0315

# 撮影 + プロンプト出力 + Ollama分析
snap --date 0315 --with-analysis

# 分析のみ（既存画像を再利用）
snap --date 0315 --analysis-only

# 空・エラーの銘柄だけ再分析
snap --date 0315 --retry-empty

# フルパスでCSVを直接指定することも可能
snap --csv /workspace/csv/20260315_約定照会.csv
```

`--date 0315` と指定すると `/workspace/csv/20260315_約定照会.csv` を自動的に使用します（年は実行時の現在年を自動取得）。

#### オプション一覧

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--date` | - | 月日4桁（例: `0315`）。`--csv` と排他 |
| `--csv` | - | 約定照会CSVのフルパス。`--date` と排他 |
| `--with-analysis` | - | Ollama分析を実行する（デフォルトはスキップ） |
| `--analysis-only` | - | 撮影をスキップして分析のみ実行 |
| `--retry-empty` | - | 空・`[ERROR]` の分析ファイルのみ再分析 |
| `--ollama-host` | `http://ollama:11434` | OllamaのURL |
| `--analysis-model` | `qwen3.5:4b` | 使用モデル |
| `--analysis-timeout` | `300` | タイムアウト秒数 |

---

### Claudeチャットで分析する（外部AI連携）

`snap --date 0315` を実行すると、撮影と同時に以下のファイルが生成されます：

- **`YYYYMMDD_まとめ_prompt.txt`** ← 全銘柄分のプロンプトを1ファイルにまとめたもの
- **`TSE_XXXX_1m_YYYYMMDD_prompt.txt`** ← 銘柄ごとの個別プロンプト

**Claude（claude.ai）に渡す手順：**

1. `20260315_まとめ_prompt.txt` をテキストとして添付（または中身をコピペ）
2. 各銘柄の `.png` を複数選択して一緒に添付
3. 「分析してください」と送信

> 銘柄ごとに個別に依頼する場合は `_prompt.txt` の `=== USER PROMPT ===` 以降をコピペし、対応する `.png` を添付してください（`=== SYSTEM PROMPT ===` 部分はチャットUIでは不要）。

---

### export_prompt.py — 単体利用

`batch_snapshot.py` にプロンプト出力機能は統合済みですが、撮影なしでプロンプトだけ再生成したい場合は単体でも使えます。

```bash
uv run python3 /workspace/scripts/export_prompt.py --csv /workspace/csv/20260306_約定照会.csv
```

---

## 出力ファイル

```
snapshots/
└── 20260315/
    ├── TSE_5016_1m_20260315_raw.png          ← 生スクリーンショット（削除しないこと）
    ├── TSE_5016_1m_20260315.png              ← マーカー合成済み（AI分析・外部共有に使う）
    ├── TSE_5016_1m_20260315_prompt.txt       ← 銘柄ごとの個別プロンプト
    ├── TSE_5016_1m_20260315_payload.json     ← 構造化データ（API連携用）
    ├── TSE_5016_1m_20260315_analysis.txt     ← Ollama分析結果（--with-analysis 時のみ）
    └── 20260315_まとめ_prompt.txt            ← 全銘柄まとめプロンプト（Claudeへの貼り付け用）
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

**`snap` コマンドが見つからない**
`uv sync` を実行してください。`pyproject.toml` の `[project.scripts]` にエントリポイントが登録されています。

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
docker exec ollama ollama list
docker exec ollama ollama pull qwen3.5:4b
```

**AI分析が空白になる（1銘柄目）**
モデルの初回ロードに時間がかかりタイムアウトすることがあります。
`--retry-empty` で空の銘柄だけ再分析できます：
```bash
snap --date 0315 --retry-empty
```

**AI分析がタイムアウトする**
デフォルトのタイムアウトは300秒です。不足する場合は延長できます：
```bash
snap --date 0315 --with-analysis --analysis-timeout 600
```

**損益が0円と表示される**
`side`（取引）カラムと `buysell`（売買）カラムの両方が正しく読み込まれているか確認してください。
`batch_snapshot.py` は両カラムを結合して買建/売埋/売建/買埋を判定します。