# TradingView Snapshot 分析環境

**Python + Playwright** で動くシンプル構成。
証券会社の約定照会CSVから銘柄を読み取り、TradingViewの1分足チャートを自動撮影・マーカー合成・プロンプト出力する。
AI分析はClaude（claude.aiなど）にプロンプトと画像を貼り付けて行う。

---

## 構成

```text
tv-snapshot-app/
├── .devcontainer/
│   ├── devcontainer.json          # VS Code DevContainer設定（docker-compose不使用）
│   └── Dockerfile                 # Python 3.11-slim-bookworm + Playwright環境
├── pyproject.toml                 # Pythonパッケージ（uv管理）
├── scripts/
│   ├── __init__.py                # パッケージ化用（snapコマンド登録に必要）
│   ├── batch_snapshot.py          # メイン: 撮影→マーカー合成→プロンプト出力
│   └── export_prompt.py           # 外部AIエージェント用プロンプト・データ出力（単体利用可）
├── csv/                           # 約定照会CSVの置き場
├── demo-trade-input.html          # デモトレード伝票（ブラウザで開くだけで使用可能）
├── tests/
│   └── test_batch_snapshot.py     # ユニットテスト
└── snapshots/                     # 撮影画像・プロンプト出力の保存先
```

---

## セットアップ

Docker Composeは不使用。DevContainer（Dockerfile直参照）またはローカルのuv、どちらでも動作する。

### 方法A: VS Code DevContainer

`.devcontainer/devcontainer.json` は `docker-compose` を使わず `Dockerfile` を直接ビルドする構成。

1. VS Codeでフォルダを開く
2. `Ctrl+Shift+P` → `Dev Containers: Reopen in Container`
3. 初回ビルド完了後、`postCreateCommand` で `uv sync` が自動実行される

コンテナ内で確認：
```bash
uv run snap --help
```

#### venvの自動有効化（恒常化）

デフォルトでは `snap` は `.venv/bin` 配下にあり、`uv run snap ...` としないとPATHに乗らない。
毎回 `uv run` を付けたくない場合は、`~/.bashrc` に以下を追記しておくとコンテナ再起動後も自動で有効化される：

```bash
echo 'source /workspace/.venv/bin/activate' >> ~/.bashrc
```

以降は `snap --date 0315` のように直接呼び出せる。

### 方法B: ローカル環境（uvのみ、Docker不使用）

#### 1. uvのインストール（未導入の場合）

```powershell
# Windows PowerShell
irm https://astral.sh/uv/install.ps1 | iex
```

#### 2. 依存関係のインストール

リポジトリのルートで実行：

```bash
uv sync
uv run playwright install chromium --with-deps
```

`--with-deps` はLinux/WSL2で必要なシステムライブラリも合わせて入れる。Windowsネイティブ実行時は `uv run playwright install chromium` のみでよい。

#### 3. 動作確認

```bash
uv run snap --help
```

---

## デモトレード伝票（demo-trade-input）

証券会社CSV不要でデモトレードを記録 → 本番と同じスキーマのCSV出力。

### 使い方
1. `demo-trade-input.html` をブラウザで開く（ダブルクリックでOK、サーバー不要）
2. 銘柄コード・売買区分（買建/売建/売埋/買埋）・単価・数量を入力→「伝票に追加」
3. 一覧で概算損益を確認しながら複数件入力
4. 「約定照会CSVを出力」→ `{YYYY}{MMDD}_約定照会.csv` がダウンロードされる
5. そのファイルを `csv/` フォルダに置き、通常どおり実行

```bash
uv run snap --date MMDD
```

### 出力CSV仕様
列: `約定日,コード,銘柄名,取引,売買,約定数量(株/口),約定単価(円)`
既存 `load_trades_from_csv()` の候補カラム名に完全一致 → 本番CSVと区別なく処理される。

### データ保持
入力中データは `localStorage` に自動保存（ブラウザ閉じても消えない）。「全件削除」で明示的にクリアするまで残る。

---

## フォントと日本語表示

Pillow を使った画像描画で日本語を安定して表示するため、`batch_snapshot.py` 内で **Noto → DejaVu → デフォルト** の順にフォントを試すフォールバック処理を導入している。

### フォント候補（順序）
- `/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc`（Regular）
- `/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc`（Bold）
- `/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf`（Regular, フォールバック）
- `/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf`（Bold, フォールバック）
- どれも読めない場合は `ImageFont.load_default()` を返す（日本語は豆腐化する可能性あり）。

Windowsネイティブ実行時にNoto/DejaVuが見当たらない場合は、上記フォントを別途インストールするか、`_load_font_candidates()` にお使いの環境のフォントパスを追加してほしい。

### 確認コマンド
```bash
uv run python -c "
from scripts.batch_snapshot import _load_font_candidates
print('body:', _load_font_candidates(18, bold=False))
print('bold:', _load_font_candidates(20, bold=True))
"
```

### 注意点
- 絵文字は別フォント（例: `NotoColorEmoji.ttf`）が必要。
- フォントサイズやマージンによって文字がはみ出すことがある。実際のスナップでレイアウトを確認し、必要なら `MARKER_FONT_SIZE`, `PRICE_PADDING_RATIO` 等を調整すること。

---

## CSVフォーマット

証券会社の約定照会CSVをそのまま使用できる。カラム名の揺れは自動吸収される。

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

`uv sync` 後は `uv run snap` で実行できる。

```bash
# --date で自動的に csv/{年}{MMDD}_約定照会.csv を使用
uv run snap --date 0315

# フルパスでCSVを直接指定することも可能
uv run snap --csv csv/20260315_約定照会.csv
```

#### オプション一覧

| 引数 | 説明 |
|------|-----|
| `--date` | 月日4桁（例: `0315`）。`--csv` と排他 |
| `--csv` | 約定照会CSVのパス。`--date` と排他 |

---

### Claudeチャットで分析する

`snap --date 0315` を実行すると、撮影と同時に以下のファイルが生成される：

- **`YYYYMMDD_まとめ_prompt.txt`** ← 全銘柄分のプロンプトを1ファイルにまとめたもの
- **`TSE_XXXX_1m_YYYYMMDD_prompt.txt`** ← 銘柄ごとの個別プロンプト

**Claude（claude.ai）に渡す手順：**

1. `20260315_まとめ_prompt.txt` をテキストとして添付（または中身をコピペ）
2. 各銘柄の `.png` を複数選択して一緒に添付
3. 「分析してください」と送信

> 銘柄ごとに個別に依頼する場合は `_prompt.txt` の `=== USER PROMPT ===` 以降をコピペし、対応する `.png` を添付する（`=== SYSTEM PROMPT ===` 部分はチャットUIでは不要）。

---

### export_prompt.py — 単体利用

`batch_snapshot.py` にプロンプト出力機能は統合済みだが、撮影なしでプロンプトだけ再生成したい場合は単体でも使える。

```bash
uv run python3 scripts/export_prompt.py --csv csv/20260306_約定照会.csv
```

---

## 日経平均チャート撮影機能

日経平均（TVC:NI225）の1分足チャートも自動で撮影する。各銘柄のトレード分析に連動情報として利用され、以下のような分析が可能：

- 日経平均の寄付きから大引けまでの値動きの流れ
- 各銘柄との連動・乖離分析
- ボラティリティの特徴と転換点の特定
- 全銘柄のトレードを総合評価するための基準情報

日経平均画像は各銘柄の分析プロンプトに自動で組み込まれ、まとめプロンプトでは日経平均の動きを中心とした1日のトレード日誌が生成される。

---

## 出力ファイル

```
snapshots/
└── 20260315/
    ├── NI225_1m_20260315.png                 ← 日経平均チャート（連動分析用）
    ├── TSE_5016_1m_20260315_raw.png          ← 生スクリーンショット
    ├── TSE_5016_1m_20260315.png              ← マーカー合成済み（AI分析・外部共有に使う）
    ├── TSE_5016_1m_20260315_prompt.txt       ← 銘柄ごとの個別プロンプト
    ├── TSE_5016_1m_20260315_payload.json     ← 構造化データ（API連携用）
    └── 20260315_まとめ_prompt.txt            ← 全銘柄まとめプロンプト（Claudeへの貼り付け用）
```

---

## まとめプロンプト構造

まとめプロンプトでは日経平均の動きを中心に、各銘柄のトレードを総合評価する。
プロンプト構造は以下の4セクションで構成される：

1. **日経平均の動き** - 寄付きから大引けまでの全体的な値動きの流れ
2. **銘柄別サマリー** - 各銘柄の日経との連動・チャートパターン・約定タイミング評価
3. **本日の総合評価** - 全銘柄合算の損益合計と反省点
4. **Obsidian用トレード日誌** - 見出し・箇条書き・表のみ使用したMarkdown形式

---

## チャートのダークモード

TradingViewのURLに `&theme=dark` を付加している（デフォルト）。
ライトモードに戻す場合は `batch_snapshot.py` 冒頭の定数を編集する：

```python
TV_URL_TEMPLATE = (
    "https://www.tradingview.com/chart/?symbol={symbol}"
    "&interval=1"
    # "&theme=dark"  ← コメントアウトでライトモードに戻す
)
```

---

## マーカー位置のズレ調整

チャートの価格レンジはCSVの約定価格から推定している。
ローソク足とマーカーがずれる場合は `PRICE_PADDING_RATIO` を調整する：

```python
# batch_snapshot.py 冒頭の定数
PRICE_PADDING_RATIO = 0.20  # 大きくするほどマーカーが中央寄りになる
```

---

## 損益計算ロジック

`estimate_pnl()` はロング・ショートを別スタックでFIFO管理する。

| 取引種別 | 計算式 |
|---------|--------|
| ロング（買建→売埋） | 売埋価格 − 買建価格 |
| ショート（売建→買埋） | 売建価格 − 買埋価格 |

未決済建玉がある場合は銘柄・数量・建値平均も出力される。

---

## テスト

```bash
uv run pytest tests/ -v
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
`uv sync` を実行する。`pyproject.toml` の `[project.scripts]` にエントリポイントが登録されている。`uv run snap ...` の形で呼び出すか、`~/.bashrc` に `source /workspace/.venv/bin/activate` を追記して恒常的に有効化する。

**Playwrightのブラウザが見つからない**
```bash
uv run playwright install chromium --with-deps
```

**DevContainerのビルドがPlaywrightエラーで失敗する**
ベースイメージが `python:3.11-slim`（Debian trixie）だとフォントパッケージが見つからずエラーになる。
`Dockerfile` の1行目が以下になっているか確認する：
```dockerfile
FROM python:3.11-slim-bookworm
```

**CSVを読み込んで「0 銘柄×日付を処理」と表示される**
約定単価にカンマ（`3,969.0`）が含まれていて数値変換に失敗している可能性がある。
また約定日カラムに時刻（`2026/03/06 09:03:40`）が含まれていても自動で日付部分のみ抽出する。
どちらも現行スクリプトで対応済み。

**損益が0円と表示される**
`side`（取引）カラムと `buysell`（売買）カラムの両方が正しく読み込まれているか確認する。
`batch_snapshot.py` は両カラムを結合して買建/売埋/売建/買埋を判定する。