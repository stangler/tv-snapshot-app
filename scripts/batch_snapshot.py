"""
batch_snapshot.py
-----------------
TradingView 1分足チャート 一括撮影 + プロンプト出力 + Ollama AI分析

使い方:
  # 撮影 + プロンプト出力（デフォルト）
  snap --date 0315

  # 撮影 + プロンプト出力 + AI分析
  snap --date 0315 --with-analysis

  # 既存の撮影済み画像だけ分析し直す
  snap --date 0315 --analysis-only

  # 空・エラーの分析ファイルのみ再分析
  snap --date 0315 --retry-empty
"""

import argparse
import base64
import io
import json
import os
import sys
import time
from collections import defaultdict
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

TV_URL_TEMPLATE = (
    "https://www.tradingview.com/chart/?symbol={symbol}"
    "&interval=1"
    "&theme=dark"
)

CHART_LEFT_PX   = 60
CHART_RIGHT_PX  = 1860
CHART_TOP_PX    = 60
CHART_BOTTOM_PX = 940
PRICE_PADDING_RATIO = 0.20

MARKER_FONT_SIZE = 28
MARKER_COLOR_BUY  = (0, 180, 80)
MARKER_COLOR_SELL = (220, 50, 50)
MARKER_COLOR_NAN  = (180, 180, 30)

DEFAULT_OLLAMA_HOST      = "http://ollama:11434"
DEFAULT_ANALYSIS_MODEL   = "qwen3.5:4b"
DEFAULT_ANALYSIS_TIMEOUT = 300

ANALYSIS_PROMPT_TEMPLATE = """\
STRICT RULE: You MUST respond in Japanese ONLY.
FORBIDDEN: Chinese characters that are not also Japanese kanji (e.g. 趋势, 买, 下行, 显示 are FORBIDDEN).
FORBIDDEN: English words mixed in Japanese sentences.
If you are unsure of a word, use katakana or rephrase in plain Japanese.

You are a Japanese stock day-trading analyst.

This chart shows a Japanese stock ({symbol}) 1-minute candlestick chart on {date}.
▲（緑）= 買いエントリー（買建・買埋）
▽（赤）= 売りエントリー（売建・売埋）

【約定一覧】（全{trade_count}件）
{trade_table}

{pnl_text}

以下の4項目を【必ず自然な日本語のみ】で回答してください。中国語・英語は一切使わないこと。
約定一覧のデータとチャート画像を照合しながら分析してください。

1. チャートの全体的なトレンド（上昇・下降・横ばい）
2. 約定タイミングの評価（各約定について良かった点・改善点）
3. チャートパターンの有無（例：ダブルトップ、フラッグ、レンジブレイク等）
4. 次回トレードへのアドバイス
"""

EXPORT_SYSTEM_PROMPT = """\
あなたは日本株のデイトレード専門のアナリストです。
提供されるチャート画像と約定データをもとに、トレードの評価と改善提案を行います。

出力は必ず以下の構造で、日本語で答えてください：

## 1. チャート概況
- トレンド方向（上昇 / 下降 / 横ばい）
- 値幅・ボラティリティの特徴

## 2. 約定タイミング評価
各約定について「良かった点」または「改善点」を具体的に述べてください。

## 3. パターン分析
チャートに見られるテクニカルパターン（あれば）を挙げてください。
例: ダブルトップ、フラッグ、V字回復、レンジブレイク等

## 4. 損益評価
約定データから概算損益を計算し、評価してください。

## 5. 次回へのアドバイス
このトレードの反省点と、同じ銘柄・相場環境での次回戦略を提案してください。
"""

EXPORT_USER_PROMPT_TEMPLATE = """\
以下は {date} の {symbol}（{symbol_name}）の1分足チャートと約定記録です。

【約定一覧】
{trade_table}

{pnl_text}

【補足】
- マーカー凡例: ▲ = 買建/買埋（緑）、▽ = 売埋/売建（赤）
- 三角形の先端が約定価格の位置を示しています
- 添付画像: {image_path}

上記チャート画像を分析し、指定のフォーマットで評価してください。
"""


# ──────────────────────────────────────────────
# 約定データ整形・損益計算
# ──────────────────────────────────────────────
def build_trade_table(trades: pd.DataFrame) -> str:
    long_stack  = []
    short_stack = []
    row_pnl     = {}

    for i, row in trades.iterrows():
        side = (
            str(row.get("side",    "")) + " " +
            str(row.get("buysell", ""))
        ).strip()
        price = float(row.get("price", 0))
        try:
            qty = int(float(str(row.get("qty", 0)).replace(",", "")))
        except Exception:
            qty = 0

        if "買建" in side:
            long_stack.append((price, qty))
            row_pnl[i] = "-"
        elif "売埋" in side:
            realized = 0.0
            remaining = qty
            while remaining > 0 and long_stack:
                bp, bq = long_stack.pop(0)
                matched = min(remaining, bq)
                realized += (price - bp) * matched
                remaining -= matched
                if bq > matched:
                    long_stack.insert(0, (bp, bq - matched))
            sign = "+" if realized >= 0 else ""
            mark = "✅" if realized > 0 else "❌" if realized < 0 else "±"
            row_pnl[i] = f"{sign}{realized:,.0f}円 {mark}"
        elif "売建" in side:
            short_stack.append((price, qty))
            row_pnl[i] = "-"
        elif "買埋" in side:
            realized = 0.0
            remaining = qty
            while remaining > 0 and short_stack:
                sp, sq = short_stack.pop(0)
                matched = min(remaining, sq)
                realized += (sp - price) * matched
                remaining -= matched
                if sq > matched:
                    short_stack.insert(0, (sp, sq - matched))
            sign = "+" if realized >= 0 else ""
            mark = "✅" if realized > 0 else "❌" if realized < 0 else "±"
            row_pnl[i] = f"{sign}{realized:,.0f}円 {mark}"
        else:
            row_pnl[i] = "-"

    lines = [f"{'No':>3}  {'取引':8}  {'売買':4}  {'約定単価':>10}  {'数量':>6}  損益（概算）"]
    lines.append("-" * 62)
    for i, row in trades.iterrows():
        side    = str(row.get("side",    "-"))
        buysell = str(row.get("buysell", "-"))
        price   = row.get("price", 0)
        qty     = str(row.get("qty", "-"))
        pnl     = row_pnl.get(i, "-")
        lines.append(
            f"{i+1:>3}  {side:8}  {buysell:4}  ¥{float(price):>10,.1f}  {qty:>5}株  {pnl}"
        )
    return "\n".join(lines)


def estimate_pnl(trades: pd.DataFrame) -> str:
    long_stack  = []
    short_stack = []
    realized    = 0.0

    for _, row in trades.iterrows():
        side = (
            str(row.get("side",    "")) + " " +
            str(row.get("buysell", ""))
        ).strip()
        price   = float(row.get("price", 0))
        qty_raw = row.get("qty", 0)
        try:
            qty = int(float(str(qty_raw).replace(",", "")))
        except Exception:
            qty = 0

        if "買建" in side:
            long_stack.append((price, qty))
        elif "売埋" in side:
            remaining = qty
            while remaining > 0 and long_stack:
                bp, bq = long_stack.pop(0)
                matched = min(remaining, bq)
                realized += (price - bp) * matched
                remaining -= matched
                if bq > matched:
                    long_stack.insert(0, (bp, bq - matched))
        elif "売建" in side:
            short_stack.append((price, qty))
        elif "買埋" in side:
            remaining = qty
            while remaining > 0 and short_stack:
                sp, sq = short_stack.pop(0)
                matched = min(remaining, sq)
                realized += (sp - price) * matched
                remaining -= matched
                if sq > matched:
                    short_stack.insert(0, (sp, sq - matched))

    sign   = "+" if realized >= 0 else ""
    result = f"【概算実現損益】 {sign}{realized:,.0f}円"

    if long_stack:
        lq = sum(q for _, q in long_stack)
        lp = sum(p * q for p, q in long_stack) / lq
        result += f"\n【未決済ロング建玉】 {lq}株（建値平均 ¥{lp:,.1f}）"
    if short_stack:
        sq2 = sum(q for _, q in short_stack)
        sp2 = sum(p * q for p, q in short_stack) / sq2
        result += f"\n【未決済ショート建玉】 {sq2}株（建値平均 ¥{sp2:,.1f}）"

    return result


def build_prompt(trades: pd.DataFrame, symbol: str, date_str: str) -> str:
    prompt = ANALYSIS_PROMPT_TEMPLATE.format(
        symbol      = symbol,
        date        = date_str,
        trade_count = len(trades),
        trade_table = build_trade_table(trades),
        pnl_text    = estimate_pnl(trades),
    )
    return prompt + "\n/no_think"


# ──────────────────────────────────────────────
# プロンプト・JSONエクスポート
# ──────────────────────────────────────────────
def export_prompt_and_payload(
    symbol: str,
    date_str: str,
    trades: pd.DataFrame,
    image_path: Path,
    out_dir: Path,
    safe_date: str,
):
    """プロンプトテキストとJSONペイロードをファイルに書き出す"""
    symbol_name = str(trades["symbol_name"].iloc[0]) \
        if "symbol_name" in trades.columns else symbol

    image_note = str(image_path) if image_path.exists() \
        else "※ 画像未生成。撮影後に再実行してください。"

    trade_table = build_trade_table(trades)
    pnl_text    = estimate_pnl(trades)

    user_prompt = EXPORT_USER_PROMPT_TEMPLATE.format(
        date        = date_str,
        symbol      = symbol,
        symbol_name = symbol_name,
        trade_table = trade_table,
        pnl_text    = pnl_text,
        image_path  = image_note,
    )

    # プロンプトテキスト出力
    prompt_path = out_dir / f"TSE_{symbol}_1m_{safe_date}_prompt.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write("=== SYSTEM PROMPT ===\n")
        f.write(EXPORT_SYSTEM_PROMPT)
        f.write("\n\n=== USER PROMPT ===\n")
        f.write(user_prompt)
    print(f"  📄 プロンプト出力 → {prompt_path.name}")

    # JSONペイロード出力
    payload = {
        "generated_at":  datetime.now().isoformat(),
        "symbol":        symbol,
        "symbol_name":   symbol_name,
        "date":          date_str,
        "image_path":    str(image_path),
        "system_prompt": EXPORT_SYSTEM_PROMPT,
        "user_prompt":   user_prompt,
        "trades": [
            {
                "no":      int(i) + 1,
                "side":    str(row.get("side",    "")),
                "buysell": str(row.get("buysell", "")),
                "price":   float(row.get("price", 0)),
                "qty":     str(row.get("qty",     "")),
                "time":    str(row.get("time",    "")),
            }
            for i, row in trades.iterrows()
        ],
        "pnl_summary": pnl_text,
    }
    json_path = out_dir / f"TSE_{symbol}_1m_{safe_date}_payload.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"  📦 JSONペイロード出力 → {json_path.name}")


def export_summary_prompt(groups_data: list, safe_date: str, out_dir: Path):
    """全銘柄分のプロンプトを1ファイルにまとめて出力する"""
    summary_path = out_dir / f"{safe_date}_まとめ_prompt.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"=== {safe_date} 全銘柄まとめ分析依頼 ===\n")
        f.write(f"銘柄数: {len(groups_data)}\n\n")
        for symbol, prompt_path, image_path in groups_data:
            f.write(f"{'='*60}\n")
            f.write(f"【{symbol}】\n")
            f.write(f"画像: {image_path}\n")
            f.write(f"{'='*60}\n")
            if prompt_path.exists():
                f.write(prompt_path.read_text(encoding="utf-8"))
            f.write("\n\n")
    print(f"\n📋 まとめプロンプト出力 → {summary_path.name}")


# ──────────────────────────────────────────────
# CSVパース
# ──────────────────────────────────────────────
def load_trades_from_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()

    rename_map = {}
    col_lower = {c.lower(): c for c in df.columns}

    candidates = {
        "symbol":      ["銘柄コード", "コード", "symbol", "code"],
        "symbol_name": ["銘柄名", "name"],
        "date":        ["約定日", "date", "日付"],
        "time":        ["約定時刻", "約定時間", "time", "時刻"],
        "price":       ["約定単価(円)", "約定単価", "価格", "price", "単価", "建単価(円)", "建単価"],
        "qty":         ["約定数量(株/口)", "約定数量", "数量", "qty", "quantity"],
        "side":        ["取引", "side"],
        "buysell":     ["売買", "buysell"],
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
    df["date"]   = df["date"].astype(str).str.extract(r"(\d{4}[/\-]\d{2}[/\-]\d{2})")[0]

    return df


def group_by_symbol_date(df: pd.DataFrame):
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
            time.sleep(wait_sec)
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
    if price_max == price_min:
        return (CHART_TOP_PX + CHART_BOTTOM_PX) // 2
    ratio = (price_max - price) / (price_max - price_min)
    return int(CHART_TOP_PX + ratio * (CHART_BOTTOM_PX - CHART_TOP_PX))


def draw_markers(image_path: Path, trades: pd.DataFrame, out_path: Path):
    img = Image.open(image_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                                  MARKER_FONT_SIZE)
    except Exception:
        font = ImageFont.load_default()

    prices = trades["price"].dropna().tolist()
    if not prices:
        img.save(str(out_path))
        return

    p_min = min(prices)
    p_max = max(prices)
    pad = (p_max - p_min) * PRICE_PADDING_RATIO if p_max != p_min else p_min * 0.05
    price_min = p_min - pad
    price_max = p_max + pad

    total = len(trades)
    chart_width = CHART_RIGHT_PX - CHART_LEFT_PX

    for i, row in trades.iterrows():
        price    = row.get("price")
        buysell  = str(row.get("buysell", "")).strip()
        side     = str(row.get("side",    "")).strip()
        combined = (buysell + " " + side).strip()

        if pd.isna(price):
            continue

        x = int(CHART_LEFT_PX + (i / max(total - 1, 1)) * chart_width)
        y = price_to_y(price, price_min, price_max)

        is_buy = any(k in combined for k in ["買建", "買埋", "買", "buy", "Buy", "BUY", "long"])
        if is_buy:
            marker = "▲"
            color  = MARKER_COLOR_BUY
            text_y = y - MARKER_FONT_SIZE
        else:
            marker = "▽"
            color  = MARKER_COLOR_SELL
            text_y = y

        draw.text((x - MARKER_FONT_SIZE // 2, text_y), marker,
                  font=font, fill=color + (220,))
        label = f"{int(price)}"
        draw.text((x - MARKER_FONT_SIZE, text_y - MARKER_FONT_SIZE - 2),
                  label, font=font, fill=color + (180,))

    composite = Image.alpha_composite(img, overlay).convert("RGB")
    composite.save(str(out_path))
    print(f"  🖊  マーカー合成 → {out_path.name}")


# ──────────────────────────────────────────────
# Ollama AI分析
# ──────────────────────────────────────────────
def analyze_image_with_ollama(image_path: Path,
                               ollama_host: str = DEFAULT_OLLAMA_HOST,
                               model: str = DEFAULT_ANALYSIS_MODEL,
                               prompt: str = "",
                               timeout: int = DEFAULT_ANALYSIS_TIMEOUT) -> str:
    with open(image_path, "rb") as f:
        img_b64 = base64.b64encode(f.read()).decode("utf-8")

    payload = {
        "model":  model,
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
    out_path = image_path.with_name(image_path.stem + "_analysis.txt")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = (
        "===================================\n"
        "AI分析レポート\n"
        f"生成日時: {now}\n"
        f"対象画像: {image_path.name}\n"
        "===================================\n\n"
    )

    trade_section = "【約定一覧】\n" + build_trade_table(trades) + "\n\n"
    pnl_section   = estimate_pnl(trades) + "\n\n"
    content = header + trade_section + pnl_section + "【AI分析】\n" + analysis_text + "\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  💾 分析結果保存 → {out_path.name}")
    return out_path


# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def analysis_is_empty(analysis_path: Path) -> bool:
    if not analysis_path.exists():
        return True
    text = analysis_path.read_text(encoding="utf-8")
    marker = "【AI分析】"
    idx = text.find(marker)
    if idx == -1:
        return True
    after = text[idx + len(marker):].strip()
    return after == "" or after.startswith("[ERROR]")


def process_group(symbol: str, date_str: str, trades: pd.DataFrame, args):
    """1銘柄×1日の処理"""
    safe_date     = date_str.replace("/", "").replace("-", "")
    out_dir       = SNAPSHOT_DIR / safe_date
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name     = f"TSE_{symbol}_1m_{safe_date}"
    raw_path      = out_dir / f"{base_name}_raw.png"
    marked_path   = out_dir / f"{base_name}.png"
    analysis_path = out_dir / f"{base_name}_analysis.txt"

    print(f"\n{'='*50}")
    print(f"🏷  銘柄: {symbol}  日付: {date_str}  約定数: {len(trades)}")

    # ── --retry-empty: 分析が正常に存在する銘柄はスキップ ──
    if getattr(args, "retry_empty", False):
        if not analysis_is_empty(analysis_path):
            print("  ✅ 分析済みのためスキップ")
            return

    # ── 撮影フェーズ ──
    if not args.analysis_only and not getattr(args, "retry_empty", False):
        if not take_snapshot(symbol, date_str, raw_path):
            print("  ⚠️  撮影失敗。スキップします。")
            return
        draw_markers(raw_path, trades, marked_path)
    else:
        if not marked_path.exists():
            if raw_path.exists():
                draw_markers(raw_path, trades, marked_path)
            else:
                print(f"  ⚠️  画像が見つかりません: {marked_path}\n  撮影してから --analysis-only を使ってください。")
                return

    # ── プロンプト・JSONエクスポート（常に実行） ──
    export_prompt_and_payload(symbol, date_str, trades, marked_path, out_dir, safe_date)

    # ── 分析フェーズ ──
    if args.no_analysis:
        print("  ℹ️  分析スキップ（--with-analysis を付けると分析します）")
        return

    print(f"  🤖 Ollamaで分析中 ({args.analysis_model})...")
    prompt = build_prompt(trades, symbol, date_str)
    analysis = analyze_image_with_ollama(
        image_path  = marked_path,
        ollama_host = args.ollama_host,
        model       = args.analysis_model,
        prompt      = prompt,
        timeout     = args.analysis_timeout,
    )
    save_analysis(analysis, marked_path, trades)


def main():
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    parser = argparse.ArgumentParser(description="TradingView一括撮影 + プロンプト出力 + Ollama分析")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--csv",  help="約定照会CSVのパス（フルパス）")
    group.add_argument("--date", help="月日4桁 例: 0315 → 20260315_約定照会.csv を自動使用")

    parser.add_argument("--with-analysis",    action="store_true", help="Ollamaで分析する（デフォルトはスキップ）")
    parser.add_argument("--analysis-only",    action="store_true", help="撮影をスキップ（分析のみ）")
    parser.add_argument("--retry-empty",      action="store_true", help="空・エラーの分析ファイルのみ再分析")
    parser.add_argument("--ollama-host",      default=os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST))
    parser.add_argument("--analysis-model",   default=DEFAULT_ANALYSIS_MODEL)
    parser.add_argument("--analysis-timeout", type=int, default=DEFAULT_ANALYSIS_TIMEOUT)
    args = parser.parse_args()

    if args.date:
        year = datetime.now().year
        args.csv = f"/workspace/csv/{year}{args.date}_約定照会.csv"
        print(f"[INFO] CSVパス: {args.csv}")

    args.no_analysis = not args.with_analysis

    if args.no_analysis and args.analysis_only:
        print("❌ 分析スキップと --analysis-only は同時に指定できません")
        sys.exit(1)

    if args.retry_empty and args.no_analysis:
        print("❌ --retry-empty と分析スキップは同時に指定できません")
        sys.exit(1)

    print(f"📂 CSV読み込み: {args.csv}")
    df = load_trades_from_csv(args.csv)
    groups = group_by_symbol_date(df)
    print(f"✅ {len(groups)} 銘柄×日付 を処理します\n")

    if not args.no_analysis:
        print(f"⏳ モデルをウォームアップ中 ({args.analysis_model})...")
        warmup_url = f"{args.ollama_host.rstrip('/')}/api/generate"
        try:
            requests.post(warmup_url, json={
                "model": args.analysis_model,
                "prompt": "hi",
                "stream": False,
            }, timeout=120)
            print("✅ ウォームアップ完了\n")
        except Exception:
            print("⚠️  ウォームアップ失敗（そのまま続行）\n")

    # 日付ごとにまとめプロンプト用データを収集
    date_groups: dict = defaultdict(list)

    for symbol, date_str, trades in groups:
        process_group(symbol, date_str, trades, args)

        # まとめプロンプト用にパスを記録
        safe_date   = date_str.replace("/", "").replace("-", "")
        out_dir     = SNAPSHOT_DIR / safe_date
        prompt_path = out_dir / f"TSE_{symbol}_1m_{safe_date}_prompt.txt"
        image_path  = out_dir / f"TSE_{symbol}_1m_{safe_date}.png"
        date_groups[safe_date].append((symbol, prompt_path, image_path))

    # 日付ごとにまとめプロンプトを生成
    for safe_date, group_data in date_groups.items():
        out_dir = SNAPSHOT_DIR / safe_date
        export_summary_prompt(group_data, safe_date, out_dir)

    print(f"\n🎉 完了！保存先: {SNAPSHOT_DIR}")


if __name__ == "__main__":
    main()