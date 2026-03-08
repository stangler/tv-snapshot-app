# TradingView Snapshot + Ollama 分析環境

ZeroClawを使わず、**Python + Playwright + Ollama (llava)** だけで動くシンプル構成。
証券会社の約定照会CSVから銘柄を読み取り、TradingViewの1分足チャートを自動撮影・マーカー合成・AI分析する。

---

## 構成

```
tv-snapshot-app/
├── .devcontainer/
│   ├── devcontainer.json      # VS Code DevContainer設定
│   ├── docker-compose.yml     # ollama + devcontainer（GPU統合・1ファイルに統合済み）
│   ├── Dockerfile             # Python 3.11-slim-bookworm + Playwright環境
│   └── requirements.txt       # Pythonパッケージ
├── csv/                       # 約定照会CSVの置き場
├── scripts/
│   ├── batch_snapshot.py      # メイン: 撮影→マーカー合成→Ollama分析
│   └── export_prompt.py       # 外部AIエージェント用プロンプト・データ出力
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

初回ビルドは数分かかります。llavaのダウンロード（約4GB）も初回のみ自動実行されます。

### 3. Ollamaモデルの確認（コンテナ内）

```bash
curl http://ollama:11434/api/tags
```

`llava:7b-v1.6` が表示されればOK。表示されない場合は手動でpull：

```bash
curl http://ollama:11434/api/pull -d '{"name":"llava:7b-v1.6"}'
```

> ⚠️ `docker logs` はコンテナ外（PowerShell）から実行してください。コンテナ内からは `docker` コマンドは使えません。
> ```powershell
> docker logs ollama-init
> docker logs ollama
> ```

---

## CSVフォーマット

証券会社の約定照会CSVをそのまま使用できます。カラム名の揺れは自動吸収されます。

| 内部名 | 対応カラム名の例 | 備考 |
|--------|----------------|------|
| symbol | コード, 銘柄コード | 4〜5桁の銘柄コード（例: 5016） |
| date   | 約定日 | 時刻込み（`2026/03/06 09:03:40`）でも自動で日付部分のみ抽出 |
| price  | 約定単価(円), 約定単価 | カンマ区切り（`3,969.0`）も自動除去 |
| side   | 取引, 売買区分 | 買建/売埋/売建/買埋 を判定 |
| qty    | 約定数量(株/口), 約定数量 | 任意 |
| time   | 約定時刻, 約定時間 | 任意 |

---

## 使い方

### batch_snapshot.py — 撮影・マーカー合成・Ollama分析

```bash
# 通常: 撮影 → マーカー合成 → Ollama(llava:7b-v1.6)分析 を一気通貫
python3 /workspace/scripts/batch_snapshot.py --csv /workspace/csv/20260306_約定照会.csv

# 撮影のみ（分析スキップ）
python3 /workspace/scripts/batch_snapshot.py --csv /workspace/csv/20260306_約定照会.csv --no-analysis

# 分析のみ（既存画像を再分析）
python3 /workspace/scripts/batch_snapshot.py --csv /workspace/csv/20260306_約定照会.csv --analysis-only
```

オプション一覧：

| 引数 | デフォルト | 説明 |
|------|-----------|------|
| `--csv` | 必須 | 約定照会CSVのパス |
| `--no-analysis` | - | Ollama分析をスキップ |
| `--analysis-only` | - | 撮影をスキップして分析のみ実行 |
| `--ollama-host` | `http://ollama:11434` | OllamaのURL |
| `--analysis-model` | `llava:7b-v1.6` | 使用モデル |
| `--analysis-timeout` | `120` | タイムアウト秒数 |

---

### export_prompt.py — 外部AIエージェント用の出力

Claude / GPT-4o など外部のエージェントに分析させる場合に使う。
API呼び出しは行わず、プロンプトとデータをファイルに書き出すだけ。

```bash
python3 /workspace/scripts/export_prompt.py --csv /workspace/csv/20260306_約定照会.csv
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
    ├── TSE_5016_1m_20260306_raw.png          ← 生スクリーンショット（中間ファイル）
    ├── TSE_5016_1m_20260306.png              ← マーカー合成済み（AI分析・外部共有に使う）
    ├── TSE_5016_1m_20260306_analysis.txt     ← Ollama分析結果
    ├── TSE_5016_1m_20260306_prompt.txt       ← 外部エージェント用プロンプト全文
    └── TSE_5016_1m_20260306_payload.json     ← 構造化データ（API連携用）
```

`_raw.png` は `--analysis-only` 再実行時にマーカーを再合成するために使います。削除しないでください。

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

`batch_snapshot.py` と `export_prompt.py` の両方の `estimate_pnl()` はロング・ショートを別スタックでFIFO管理します。
`取引`（side）と `売買`（buysell）カラムを結合して判定します。

| 取引種別 | 計算式 |
|---------|--------|
| ロング（買建→売埋） | 売埋価格 − 買建価格 |
| ショート（売建→買埋） | 売建価格 − 買埋価格 |

未決済建玉がある場合は銘柄・数量・建値平均も出力されます。

---

## インストール済みモデル

| モデル名 | 推奨 | 備考 |
|---|---|---|
| `llava:latest` | - | 解像度低め・旧デフォルト |
| `llava:7b-v1.6` | ✅ **現在のデフォルト** | 日本語安定・解像度中 |
| `minicpm-v` | - | 高解像度対応だが中国語混入の問題あり |

モデルの確認（PowerShellから）：
```powershell
docker exec ollama ollama list
```

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
llava:7b-v1.6がまだダウンロードされていない可能性があります：
```bash
curl http://ollama:11434/api/tags                                        # モデル一覧確認
curl http://ollama:11434/api/pull -d '{"name":"llava:7b-v1.6"}'         # 手動pull
```

**分析結果が中国語混じりになる**
`minicpm-v`（中国製モデル）を使っている場合に発生します。`llava:7b-v1.6` を使ってください：
```bash
curl http://ollama:11434/api/pull -d '{"name":"llava:7b-v1.6"}'
```
または実行時に `--analysis-model llava:7b-v1.6` を指定してください。