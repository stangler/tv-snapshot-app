"""
batch_snapshot.py
-----------------
TradingView 1分足チャート 一括撮影 + Ollama(llava) AI分析

使い方:
  # 撮影 → マーカー合成 → AI分析まで一気通貫
  python3 /workspace/scripts/batch_snapshot.py --csv /workspace/20260306_約定照会.csv

  # 既存の撮影済み画像だけ分析し直す
  python3 /workspace/scripts/batch_snapshot.py --csv /workspace/20260306_約定照会.csv --analysis-only

  # 撮影のみ（分析スキップ）
  python3 /workspace/scripts/batch_snapshot.py --csv /workspace/20260306_約定照会.csv --no-analysis
"""

import argparse
import base64
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────
SNAPSHOT_DIR = Path("/workspace/snapshots")

# TradingViewチャートのURL雛形（1分足）
# symbol例: "TSE:5016"
TV_URL_TEMPLATE = (
    "https://www.tradingview.com/chart/?symbol={symbol}"
    "&interval=1"
    "&theme=dark"
)

# マーカー描画パラメータ
CHART_LEFT_PX   = 60    # チャートエリア左端（ピクセル）
CHART_RIGHT_PX  = 1860  # チャートエリア右端（ピクセル）
CHART_TOP_PX    = 60    # チャートエリア上端（ピクセル）
CHART_BOTTOM_PX = 940   # チャートエリア下端（ピクセル）
PRICE_PADDING_RATIO = 0.20  # 価格レンジへのパディング（ズレが大きければ増やす）

MARKER_FONT_SIZE = 28
MARKER_COLOR_BUY  = (0, 180, 80)    # 緑
MARKER_COLOR_SELL = (220, 50, 50)   # 赤
MARKER_COLOR_NAN  = (180, 180, 30)  # ナンピン括弧色

# Ollamaデフォルト設定
DEFAULT_OLLAMA_HOST    = "http://ollama:11434"
DEFAULT_ANALYSIS_MODEL = "llava:latest"
DEFAULT_ANALYSIS_TIMEOUT = 120

DEFAULT_ANALYSIS_PROMPT = """\
あなたは株式トレードの専門家です。
このチャート画像は日本株の1分足チャートで、三角形のマーカーが約定を示しています。
▲ = 買い（緑）、▽ = 売り（赤）

以下の点を分析してください：
1. チャートの全体的なトレンド（上昇・下降・横ばい）
2. 約定タイミングの評価（良かった点・改善点）
3. チャートパターンの有無（例: ダブルトップ、フラッグ等）
4. 次回トレードへのアドバイス

日本語で簡潔に答えてください。
"""


# ──────────────────────────────────────────────
# CSVパース
# ──────────────────────────────────────────────
def load_trades_from_csv(csv_path: str) -> pd.DataFrame:
    """
    証券会社の約定照会CSVを読み込む。
    カラム名は証券会社によって異なるため、柔軟にマッピングする。
    期待するカラム: symbol, date, time, price, qty, side
    """
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()

    # カラム名の揺れを吸収するマッピング（適宜追加）
    rename_map = {}
    col_lower = {c.lower(): c for c in df.columns}

    candidates = {
        "symbol": ["銘柄コード", "コード", "symbol", "code"],
        "date":   ["約定日", "date", "日付"],
        "time":   ["約定時刻", "約定時間", "time", "時刻"],
        "price":  ["約定単価(円)", "約定単価", "価格", "price", "単価", "建単価(円)", "建単価"],
        "qty":    ["約定数量(株/口)", "約定数量", "数量", "qty", "quantity"],
        "side":   ["取引", "売買区分", "売買", "side", "buysell"],
    }

    for target, keys in candidates.items():
        for k in keys:
            if k in col_lower:
                rename_map[col_lower[k]] = target
                break

    df = df.rename(columns=rename_map)

    required = {"symbol", "date", "price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CSVに必要なカラムが見つかりません: {missing}\n現在のカラム: {list(df.columns)}")

    df["price"] = pd.to_numeric(df["price"].astype(str).str.replace(",", "", regex=False), errors="coerce")
    df = df.dropna(subset=["price"])
    df["symbol"] = df["symbol"].astype(str).str.strip()
    # 約定日に時刻が含まれる場合（例: 2026/03/06 09:03:40）は日付部分だけ抽出
    df["date"] = df["date"].astype(str).str.extract(r"(\d{4}[/\-]\d{2}[/\-]\d{2})")[0]

    return df


def group_by_symbol_date(df: pd.DataFrame):
    """銘柄×日付でグループ化して返す"""
    groups = []
    for (symbol, date), sub in df.groupby(["symbol", "date"]):
        groups.append((str(symbol), str(date), sub.reset_index(drop=True)))
    return groups


# ──────────────────────────────────────────────
# TradingViewスクリーンショット
# ──────────────────────────────────────────────
def take_snapshot(symbol: str, date_str: str, out_path: Path,
                  width: int = 1920, height: int = 1080,
                  wait_sec: int = 8) -> bool:
    """Playwrightでチャートをスクリーンショット"""
    # 市場プレフィックスを付与（4〜5桁数字 → TSE:）
    if symbol.isdigit() and len(symbol) in (4, 5):
        tv_symbol = f"TSE:{symbol}"
    else:
        tv_symbol = symbol

    url = TV_URL_TEMPLATE.format(symbol=tv_symbol)
    print(f"  📷 {tv_symbol} → {url}")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(url, wait_until="networkidle", timeout=30000)
            time.sleep(wait_sec)  # チャート描画待ち
            page.screenshot(path=str(out_path))
            browser.close()
        return True
    except Exception as e:
        print(f"  ❌ スクリーンショット失敗: {e}")
        return False


# ──────────────────────────────────────────────
# マーカー描画
# ──────────────────────────────────────────────
def price_to_y(price: float, price_min: float, price_max: float) -> int:
    """約定価格をチャートのY座標に変換"""
    if price_max == price_min:
        return (CHART_TOP_PX + CHART_BOTTOM_PX) // 2
    ratio = (price_max - price) / (price_max - price_min)
    return int(CHART_TOP_PX + ratio * (CHART_BOTTOM_PX - CHART_TOP_PX))


def draw_markers(image_path: Path, trades: pd.DataFrame, out_path: Path):
    """
    約定マーカー（▲▽）をチャート画像に重ねて保存する。
    trades には少なくとも price, side, time カラムが必要。
    """
    img = Image.open(image_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                                  MARKER_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()

    # 価格レンジ推定
    prices = trades["price"].dropna().tolist()
    if not prices:
        img.save(str(out_path))
        return

    p_min = min(prices)
    p_max = max(prices)
    pad = (p_max - p_min) * PRICE_PADDING_RATIO if p_max != p_min else p_min * 0.05
    price_min = p_min - pad
    price_max = p_max + pad

    # 時刻→X座標マッピング
    total = len(trades)
    chart_width = CHART_RIGHT_PX - CHART_LEFT_PX

    for i, row in trades.iterrows():
        price = row.get("price")
        side  = str(row.get("side", "")).strip()

        if pd.isna(price):
            continue

        x = int(CHART_LEFT_PX + (i / max(total - 1, 1)) * chart_width)
        y = price_to_y(price, price_min, price_max)

        # 売買区分で記号・色を決定
        is_buy = any(k in side for k in ["買", "buy", "Buy", "BUY", "long"])
        if is_buy:
            marker = "▲"
            color  = MARKER_COLOR_BUY
            # 先端（上頂点）がY座標
            text_y = y - MARKER_FONT_SIZE
        else:
            marker = "▽"
            color  = MARKER_COLOR_SELL
            # 先端（下頂点）がY座標
            text_y = y

        draw.text((x - MARKER_FONT_SIZE // 2, text_y), marker,
                  font=font, fill=color + (220,))

        # 価格ラベル
        label = f"{int(price)}"
        draw.text((x - MARKER_FONT_SIZE, text_y - MARKER_FONT_SIZE - 2),
                  label, font=font, fill=color + (180,))

    composite = Image.alpha_composite(img, overlay).convert("RGB")
    composite.save(str(out_path))
    print(f"  🖊  マーカー合成 → {out_path.name}")


# ──────────────────────────────────────────────
# Ollama AI分析
# ──────────────────────────────────────────────
def analyze_image_with_llava(image_path: Path,
                              ollama_host: str = DEFAULT_OLLAMA_HOST,
                              model: str = DEFAULT_ANALYSIS_MODEL,
                              prompt: str = DEFAULT_ANALYSIS_PROMPT,
                              timeout: int = DEFAULT_ANALYSIS_TIMEOUT) -> str:
    """画像をbase64エンコードしてOllama API(/api/generate)に送信し、分析テキストを返す"""
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model": model,
        "prompt": prompt,
        "images": [img_b64],
        "stream": False,
    }

    url = f"{ollama_host.rstrip('/')}/api/generate"
    try:
        resp = requests.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json().get("response", "（レスポンスなし）")
    except requests.exceptions.ConnectionError:
        return f"[ERROR] Ollamaに接続できません: {url}"
    except requests.exceptions.Timeout:
        return f"[ERROR] タイムアウト ({timeout}秒)"
    except Exception as e:
        return f"[ERROR] {e}"


def save_analysis(analysis_text: str, image_path: Path, trades: pd.DataFrame):
    """分析結果を {画像ファイル名}_analysis.txt に保存する"""
    out_path = image_path.with_name(image_path.stem + "_analysis.txt")

    header = f"""===================================
AI分析レポート
生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
対象画像: {image_path.name}
===================================

【約定一覧】
"""
    trade_lines = ""
    for _, row in trades.iterrows():
        cols = [str(row.get(c, "")) for c in ["date", "time", "side", "price", "qty"] if c in row.index]
        trade_lines += "  " + " | ".join(cols) + "\n"

    content = header + trade_lines + "\n【AI分析】\n" + analysis_text + "\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  💾 分析結果保存 → {out_path.name}")
    return out_path


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def process_group(symbol: str, date_str: str, trades: pd.DataFrame, args):
    """1銘柄×1日の処理"""
    # フォルダ作成
    safe_date = date_str.replace("/", "").replace("-", "")
    out_dir = SNAPSHOT_DIR / safe_date
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = f"TSE_{symbol}_1m_{safe_date}"
    raw_path     = out_dir / f"{base_name}_raw.png"
    marked_path  = out_dir / f"{base_name}.png"

    print(f"\n{'='*50}")
    print(f"🏷  銘柄: {symbol}  日付: {date_str}  約定数: {len(trades)}")

    # ── 撮影フェーズ ──
    if not args.analysis_only:
        if not take_snapshot(symbol, date_str, raw_path):
            print("  ⚠️  撮影失敗。スキップします。")
            return

        draw_markers(raw_path, trades, marked_path)
    else:
        # analysis-only: marked_path が存在するか確認
        if not marked_path.exists():
            # raw があればマーカー合成だけやり直す
            if raw_path.exists():
                draw_markers(raw_path, trades, marked_path)
            else:
                print(f"  ⚠️  画像が見つかりません: {marked_path}\n  撮影してから --analysis-only を使ってください。")
                return

    # ── 分析フェーズ ──
    if args.no_analysis:
        print("  ℹ️  --no-analysis 指定のため分析をスキップ")
        return

    print(f"  🤖 Ollamaで分析中 ({args.analysis_model})...")
    analysis = analyze_image_with_llava(
        image_path   = marked_path,
        ollama_host  = args.ollama_host,
        model        = args.analysis_model,
        prompt       = args.analysis_prompt,
        timeout      = args.analysis_timeout,
    )
    save_analysis(analysis, marked_path, trades)


def main():
    parser = argparse.ArgumentParser(description="TradingView一括撮影 + Ollama分析")
    parser.add_argument("--csv", required=True, help="約定照会CSVのパス")
    parser.add_argument("--no-analysis",    action="store_true", help="分析をスキップ（撮影のみ）")
    parser.add_argument("--analysis-only",  action="store_true", help="撮影をスキップ（分析のみ）")
    parser.add_argument("--ollama-host",    default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST))
    parser.add_argument("--analysis-model", default=DEFAULT_ANALYSIS_MODEL)
    parser.add_argument("--analysis-prompt",default=DEFAULT_ANALYSIS_PROMPT)
    parser.add_argument("--analysis-timeout", type=int, default=DEFAULT_ANALYSIS_TIMEOUT)
    args = parser.parse_args()

    if args.no_analysis and args.analysis_only:
        print("❌ --no-analysis と --analysis-only は同時に指定できません")
        sys.exit(1)

    print(f"📂 CSV読み込み: {args.csv}")
    df = load_trades_from_csv(args.csv)
    groups = group_by_symbol_date(df)
    print(f"✅ {len(groups)} 銘柄×日付 を処理します\n")

    for symbol, date_str, trades in groups:
        process_group(symbol, date_str, trades, args)

    print(f"\n🎉 完了！保存先: {SNAPSHOT_DIR}")


if __name__ == "__main__":
    main()