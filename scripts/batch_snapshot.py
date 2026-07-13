"""
batch_snapshot.py
-----------------
TradingView 1分足チャート 一括撮影 + プロンプト出力

使い方:
  # 撮影 + プロンプト出力
  snap --date 0315
  snap --csv /path/to/約定照会.csv
"""

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

from PIL import ImageFont

def _load_font_candidates(size: int, bold: bool = False):
    """
    Noto → DejaVu の順でフォントを試し、最初に読み込めたフォントを返す。
    読み込めなければ ImageFont.load_default() を返す（日本語は豆腐化する可能性あり）。
    """
    if bold:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ]
    else:
        candidates = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size)
        except Exception:
            continue
    return ImageFont.load_default()


# ──────────────────────────────────────────────
# 定数
# ──────────────────────────────────────────────
REPO_ROOT    = Path(__file__).resolve().parent.parent
SNAPSHOT_DIR = REPO_ROOT / "snapshots"
CSV_DIR      = REPO_ROOT / "csv"

JST = timezone(timedelta(hours=9))

TV_URL_TEMPLATE = (
    "https://www.tradingview.com/chart/?symbol={symbol}"
    "&interval=1"
    "&theme=dark"
    "&timezone=Asia%2FTokyo"
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

**重要**: チャートの時刻はUTCですが、JSTの時刻で分析してください。

【補足】
- マーカー凡例: ▲ = 買建/買埋（緑）、▽ = 売埋/売建（赤）
- 三角形の先端が約定価格の位置を示しています
- 添付画像: {image_path}

上記チャート画像を分析し、指定のフォーマットで評価してください。
"""

SUMMARY_SYSTEM_PROMPT = """\
あなたは日本株のデイトレード専門のアナリストです。
その日の全銘柄の約定データとチャート画像をもとに、1日のトレードを総合評価します。

**重要**: すべてのチャート画像の時間軸はUTC表示ですが、分析は必ず**日本時間（JST）**で行ってください。
約定時刻もJST基準で評価し、日経平均の動きや銘柄のタイミングを判断すること。

出力は必ず以下の構造で、日本語のみで答えてください。
コードブロックは使わないこと。見出し・箇条書き・表のみ使用すること。

# {date} トレード日誌

## メモ
（ここに気づきや反省を記入）

## 日経平均の動き
- 寄付きから大引けの流れ・転換点・ボラティリティ

## 銘柄別サマリー
各銘柄について以下をまとめること：
- 日経との連動
- チャートパターン
- 約定タイミング評価
- 損益（概算）

## 本日の総合評価
- 全銘柄合算の損益合計
- 良かった点
- 反省点
- 翌日戦略

（このMarkdownはObsidianにそのままコピペできる形式で出力してください）
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

    prompt_path = out_dir / f"TSE_{symbol}_1m_{safe_date}_prompt.txt"
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write("=== SYSTEM PROMPT ===\n")
        f.write(EXPORT_SYSTEM_PROMPT)
        f.write("\n\n=== USER PROMPT ===\n")
        f.write(user_prompt)
    print(f"  📄 プロンプト出力 → {prompt_path.name}")

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
    """日誌フォーマットのまとめプロンプトを生成（Obsidian向け）"""
    date_str    = f"{safe_date[:4]}/{safe_date[4:6]}/{safe_date[6:]}"
    nikkei_path = out_dir / f"NI225_1m_{safe_date}.png"

    user_lines = [
        f"以下は {date_str} の全銘柄トレードデータです。",
        "",
        "【日経平均チャート画像】",
        str(nikkei_path),
        "",
        "【銘柄一覧】",
        f"{len(groups_data)}銘柄",
    ]

    for symbol, prompt_path, image_path, trades in groups_data:
        symbol_name = str(trades["symbol_name"].iloc[0]) \
            if "symbol_name" in trades.columns else symbol
        trade_table = build_trade_table(trades)
        pnl_text    = estimate_pnl(trades)

        user_lines += [
            "",
            "",
            f"### {symbol}（{symbol_name}）",
            f"チャート画像: {image_path}",
            "",
            "【約定一覧】",
            trade_table,
            "",
            pnl_text,
            "─" * 60,
        ]

    user_lines.append(
        "\n上記すべてのデータとチャート画像を総合的に分析し、"
        "指定のフォーマットで1日のトレード日誌を作成してください。"
    )

    summary_path = out_dir / f"{safe_date}_まとめ_prompt.txt"
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("=== SYSTEM PROMPT ===\n")
        f.write(SUMMARY_SYSTEM_PROMPT.format(date=date_str))
        f.write("\n\n=== USER PROMPT ===\n")
        f.write("\n".join(user_lines))
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

    # 約定日カラムから日付・時刻を分離して抽出（月・日をゼロパディングしYYYY/MM/DDに正規化）
    date_col_raw = df["date"].astype(str)
    raw_date = date_col_raw.str.extract(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})")
    df["date"] = (
        raw_date[0] + "/" +
        raw_date[1].str.zfill(2) + "/" +
        raw_date[2].str.zfill(2)
    )

    # timeカラムが未設定の場合、約定日の時刻部分（H:MM / HH:MM）を抽出
    if "time" not in df.columns or df["time"].isna().all():
        extracted_time = date_col_raw.str.extract(r"(\d{1,2}:\d{2})")[0]
        if extracted_time.notna().any():
            df["time"] = extracted_time

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

            # ── 1D ボタンをクリックして1日全体を表示 ──
            try:
                page.locator("button:has-text('1D')").first.click(timeout=3000)
                time.sleep(3)
                print("  ✅ 1Dボタンクリック成功")
            except Exception:
                print("  ⚠️  1Dボタンが見つかりません（そのまま続行）")

            page.screenshot(path=str(out_path))
            browser.close()
        return True
    except Exception as e:
        print(f"  ❌ スクリーンショット失敗: {e}")
        return False


def take_nikkei_snapshot(date_str: str, out_path: Path,
                         width: int = 1920, height: int = 1080,
                         wait_sec: int = 8) -> bool:
    url = (
        "https://www.tradingview.com/chart/?symbol=TVC:NI225"
        "&interval=1"
        "&theme=dark"
        "&timezone=Asia%2FTokyo"
    )
    print(f"  📷 TVC:NI225 → {url}")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": width, "height": height})
            page.goto(url, wait_until="networkidle", timeout=30000)
            time.sleep(wait_sec)
            try:
                page.locator("button:has-text('1D')").first.click(timeout=3000)
                time.sleep(3)
                print("  ✅ 1Dボタンクリック成功")
            except Exception:
                print("  ⚠️  1Dボタンが見つかりません（そのまま続行）")
            page.screenshot(path=str(out_path))
            browser.close()
        return True
    except Exception as e:
        print(f"  ❌ スクリーンショット失敗: {e}")
        return False


# ──────────────────────────────────────────────
# 時刻 → X座標変換
# ──────────────────────────────────────────────
MARKET_OPEN_MINUTES  = 9 * 60        # 09:00 JST
MARKET_CLOSE_MINUTES = 15 * 60 + 30  # 15:30 JST
MARKET_TOTAL_MINUTES = MARKET_CLOSE_MINUTES - MARKET_OPEN_MINUTES  # 390分


def time_to_x(time_str: str) -> int | None:
    """
    'H:MM' / 'HH:MM' 形式のJST時刻をチャートのX座標に変換する。
    9:00〜15:30 を CHART_LEFT_PX〜CHART_RIGHT_PX に線形マッピング。
    昼休み(11:30〜12:30)は簡略化してそのまま線形処理。
    """
    try:
        h, m = map(int, str(time_str).strip().split(":"))
    except Exception:
        return None
    minutes = h * 60 + m
    minutes = max(MARKET_OPEN_MINUTES, min(minutes, MARKET_CLOSE_MINUTES))
    ratio = (minutes - MARKET_OPEN_MINUTES) / MARKET_TOTAL_MINUTES
    return int(CHART_LEFT_PX + ratio * (CHART_RIGHT_PX - CHART_LEFT_PX))



def price_to_y(price: float, price_min: float, price_max: float) -> int:
    if price_max == price_min:
        return (CHART_TOP_PX + CHART_BOTTOM_PX) // 2
    ratio = (price_max - price) / (price_max - price_min)
    return int(CHART_TOP_PX + ratio * (CHART_BOTTOM_PX - CHART_TOP_PX))


def _draw_text_with_outline(draw, pos, text, font, fill, outline=(0, 0, 0, 255), width=2):
    """縁取り付きテキストを描画する"""
    x, y = pos
    for dx in range(-width, width + 1):
        for dy in range(-width, width + 1):
            if dx != 0 or dy != 0:
                draw.text((x + dx, y + dy), text, font=font, fill=outline)
    draw.text(pos, text, font=font, fill=fill)


def _draw_filled_circle(draw, cx, cy, r, fill, outline=None, outline_width=3):
    """塗りつぶし円を描画する"""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    if outline:
        draw.ellipse(
            [cx - r, cy - r, cx + r, cy + r],
            outline=outline, width=outline_width,
        )


def _draw_price_label(draw, x, y, label, font, color, above: bool):
    """価格ラベルをボックス付きで描画する"""
    bbox = draw.textbbox((0, 0), label, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = 4
    lx = x - tw // 2 - pad
    ly = (y - th - pad * 2 - 6) if above else (y + 6)
    rx, ry = lx + tw + pad * 2, ly + th + pad * 2
    draw.rounded_rectangle([lx, ly, rx, ry], radius=4,
                            fill=(0, 0, 0, 180), outline=color + (200,), width=1)
    draw.text((lx + pad, ly + pad), label, font=font, fill=color + (255,))


def _draw_trade_table(draw, trades: pd.DataFrame, font_header, font_body,
                      img_width: int, img_height: int):
    """
    トレード一覧テーブルを画像右下に描画する。
    列: No | L/S | 売買 | 時刻 | 価格 | 損益
    """
    import re as _re

    # ── P&L を行ごとに計算 ──
    long_stack:  list = []
    short_stack: list = []
    row_pnl: dict = {}

    for i, row in trades.iterrows():
        side    = str(row.get("side",    ""))
        buysell = str(row.get("buysell", ""))
        combined = (side + " " + buysell).strip()
        price = float(row.get("price", 0))
        try:
            qty = int(float(str(row.get("qty", 0)).replace(",", "")))
        except Exception:
            qty = 0

        if "買建" in combined:
            long_stack.append((price, qty))
            row_pnl[i] = None
        elif "売埋" in combined:
            realized = 0.0
            rem = qty
            while rem > 0 and long_stack:
                bp, bq = long_stack.pop(0)
                m = min(rem, bq)
                realized += (price - bp) * m
                rem -= m
                if bq > m:
                    long_stack.insert(0, (bp, bq - m))
            row_pnl[i] = realized
        elif "売建" in combined:
            short_stack.append((price, qty))
            row_pnl[i] = None
        elif "買埋" in combined:
            realized = 0.0
            rem = qty
            while rem > 0 and short_stack:
                sp, sq = short_stack.pop(0)
                m = min(rem, sq)
                realized += (sp - price) * m
                rem -= m
                if sq > m:
                    short_stack.insert(0, (sp, sq - m))
            row_pnl[i] = realized
        else:
            row_pnl[i] = None

    # ── テーブル行データを構築 ──
    COLS      = ["No", "L/S", "売買",   "時刻",  "価格",          "損益"]
    COL_W     = [42,   52,    88,       76,      128,             155]
    ROW_H     = 28
    PAD_X     = 12
    PAD_Y     = 8
    HEADER_H  = ROW_H + 6

    rows_data = []
    for i, row in trades.iterrows():
        side    = str(row.get("side",    "-"))
        buysell = str(row.get("buysell", "-"))
        combined = side + buysell
        price   = float(row.get("price", 0))
        time_val = str(row.get("time", "-")).strip()
        qty_raw  = str(row.get("qty", "-"))

        # L/S 判定（建玉の方向を表示：建てはその方向、埋めは元の建玉方向を表示）
        if "買建" in combined:
            ls_label = "L"
        elif "売建" in combined:
            ls_label = "S"
        elif "買埋" in combined:
            # 買埋 = ショートを決済（元はショート）→ 表示は S
            ls_label = "S"
        elif "売埋" in combined:
            # 売埋 = ロングを決済（元はロング）→ 表示は L
            ls_label = "L"
        else:
            if "買" in combined:
                ls_label = "L"
            elif "売" in combined:
                ls_label = "S"
            else:
                ls_label = "-"

        # 色割当を ls_label 基準にする
        if ls_label == "L":
            row_bg   = (0, 160, 70, 45)
            txt_color = (120, 255, 140, 255)
        elif ls_label == "S":
            row_bg   = (200, 40, 40, 45)
            txt_color = (255, 130, 130, 255)
        else:
            row_bg = (180, 180, 180, 45)
            txt_color = (100, 100, 100, 255)

        # 損益セル
        pnl = row_pnl.get(i)
        if pnl is None:
            pnl_str   = "-"
            pnl_color = (180, 180, 180, 255)
        else:
            sign      = "+" if pnl >= 0 else ""
            pnl_str   = f"{sign}{pnl:,.0f}円"
            pnl_color = (0, 140, 60, 255) if pnl > 0 else (200, 30, 30, 255) if pnl < 0 else (100, 100, 100, 255)

        rows_data.append({
            "cells":     [str(i + 1), ls_label, buysell, time_val,
                          f"¥{price:,.0f}", pnl_str],
            "row_bg":    row_bg,
            "txt_color": txt_color,
            "pnl_color": pnl_color,
        })

    # ── テーブルサイズ・配置（チャートエリア左下） ──
    total_w = sum(COL_W) + PAD_X * 2
    total_h = HEADER_H + len(rows_data) * ROW_H + PAD_Y * 2
    MARGIN  = 16
    tx = MARGIN                          # 左端
    ty = img_height - total_h - MARGIN  # 下端

    # 背景（白・不透明）
    draw.rounded_rectangle(
        [tx, ty, tx + total_w, ty + total_h],
        radius=6,
        fill=(255, 255, 255, 235),
        outline=(180, 180, 180, 255),
        width=1,
    )

    # ── ヘッダー行（濃いグレー背景）──
    draw.rounded_rectangle(
        [tx, ty, tx + total_w, ty + HEADER_H + PAD_Y],
        radius=6,
        fill=(55, 65, 80, 255),
    )
    hx = tx + PAD_X
    hy = ty + PAD_Y
    for col, cw in zip(COLS, COL_W):
        draw.text((hx, hy), col, font=font_header, fill=(255, 255, 255, 255))
        hx += cw

    # ヘッダー下線
    draw.line(
        [(tx, ty + HEADER_H + PAD_Y),
         (tx + total_w, ty + HEADER_H + PAD_Y)],
        fill=(180, 180, 180, 255), width=1,
    )

    # ── データ行 ──
    for ri, rd in enumerate(rows_data):
        ry = ty + HEADER_H + PAD_Y + ri * ROW_H

        # （交互ストライプを削除）行背景はテーブル全体の白背景を利用するため個別の塗りつぶしは行わない

        # 買/売の左端アクセントライン
        accent = (0, 160, 70, 220) if rd["row_bg"][0] == 0 else (200, 40, 40, 220)
        draw.rectangle([tx + 1, ry, tx + 4, ry + ROW_H - 1], fill=accent)

        rx = tx + PAD_X + 4  # アクセントライン分ずらす
        for ci, (cell, cw) in enumerate(zip(rd["cells"], COL_W)):
            if ci == 1:       # L/S 列
                color = (0, 140, 60, 255) if rd["row_bg"][0] == 0 else (190, 30, 30, 255)
            elif ci == 5:     # 損益列
                color = rd["pnl_color"]
            else:
                color = (30, 30, 30, 255)
            draw.text((rx, ry + 4), cell, font=font_body, fill=color)
            rx += cw


def draw_markers(image_path: Path, trades: pd.DataFrame, out_path: Path):
    img = Image.open(image_path).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    CIRCLE_R    = 22   # マーカー円の半径
    FONT_SIZE   = 20   # 番号フォントサイズ
    LABEL_SIZE  = 16   # 価格ラベルのフォントサイズ
    LINE_WIDTH  = 3    # エントリー→エグジット結線の太さ

    # フォント読み込みはモジュール先頭で定義した _load_font_candidates を利用する
    font_num   = _load_font_candidates(FONT_SIZE, bold=True)
    font_label = _load_font_candidates(LABEL_SIZE, bold=False)

    # 日本語対応フォント（テーブル描画用）
    TABLE_HEADER_SIZE = 20
    TABLE_BODY_SIZE   = 18
    font_tbl_header = _load_font_candidates(TABLE_HEADER_SIZE, bold=True)
    font_tbl_body   = _load_font_candidates(TABLE_BODY_SIZE, bold=False)

    prices = trades["price"].dropna().tolist()
    if not prices:
        img.save(str(out_path))
        return

    p_min = min(prices)
    p_max = max(prices)
    pad = (p_max - p_min) * PRICE_PADDING_RATIO if p_max != p_min else p_min * 0.05
    price_min = p_min - pad
    price_max = p_max + pad

    total       = len(trades)
    chart_width = CHART_RIGHT_PX - CHART_LEFT_PX

    # 時刻カラムが使えるか確認
    has_time = (
        "time" in trades.columns
        and trades["time"].notna().any()
        and trades["time"].astype(str).str.contains(r"\d+:\d+").any()
    )

    # ── 各約定のXY座標と属性を先に計算 ──
    points = []
    for i, row in trades.iterrows():
        price   = row.get("price")
        buysell = str(row.get("buysell", "")).strip()
        side    = str(row.get("side",    "")).strip()
        combined = (buysell + " " + side).strip()

        if pd.isna(price):
            continue

        # X座標: 時刻ベース優先、フォールバックはインデックス均等割り
        if has_time:
            x = time_to_x(str(row.get("time", "")))
            if x is None:
                x = int(CHART_LEFT_PX + (i / max(total - 1, 1)) * chart_width)
        else:
            x = int(CHART_LEFT_PX + (i / max(total - 1, 1)) * chart_width)

        y = price_to_y(price, price_min, price_max)
        is_buy = any(k in combined for k in ["買建", "買埋", "買", "buy", "Buy", "BUY", "long"])
        points.append({
            "no":     i + 1,
            "x":      x,
            "y":      y,
            "price":  price,
            "is_buy": is_buy,
            "color":  MARKER_COLOR_BUY if is_buy else MARKER_COLOR_SELL,
        })

    # ── エントリー→エグジット 結線（隣接する買→売 or 売→買をペアリング） ──
    used = set()
    for idx, pt in enumerate(points):
        if idx in used:
            continue
        for jdx in range(idx + 1, len(points)):
            if jdx in used:
                continue
            nxt = points[jdx]
            # 逆方向なら結線
            if pt["is_buy"] != nxt["is_buy"]:
                draw.line(
                    [(pt["x"], pt["y"]), (nxt["x"], nxt["y"])],
                    fill=(255, 255, 100, 160),
                    width=LINE_WIDTH,
                )
                used.add(idx)
                used.add(jdx)
                break

    # ── マーカー本体を描画 ──
    for pt in points:
        x, y   = pt["x"], pt["y"]
        color  = pt["color"]
        is_buy = pt["is_buy"]
        no     = pt["no"]

        # 円の縁取り（白）→ 塗りつぶし
        _draw_filled_circle(draw, x, y, CIRCLE_R,
                            fill=color + (200,),
                            outline=(255, 255, 255, 220),
                            outline_width=3)

        # ▲ or ▽ シンボル（円の中央）
        symbol = "▲" if is_buy else "▽"
        sym_bbox = draw.textbbox((0, 0), symbol, font=font_num)
        sw = sym_bbox[2] - sym_bbox[0]
        sh = sym_bbox[3] - sym_bbox[1]
        _draw_text_with_outline(
            draw,
            (x - sw // 2, y - sh // 2 - 2),
            symbol, font_num,
            fill=(255, 255, 255, 255),
            outline=(0, 0, 0, 200),
            width=1,
        )

        # 取引番号（円の右上）
        num_str = str(no)
        _draw_text_with_outline(
            draw,
            (x + CIRCLE_R - 4, y - CIRCLE_R - 4),
            num_str, font_label,
            fill=(255, 255, 100, 255),
            outline=(0, 0, 0, 255),
            width=2,
        )

        # 価格ラベル（買いは上、売りは下）
        _draw_price_label(
            draw, x, y,
            f"¥{int(pt['price']):,}",
            font_label, color,
            above=is_buy,
        )

    # ── トレードテーブルをオーバーレイに描画 ──
    _draw_trade_table(
        draw, trades,
        font_tbl_header, font_tbl_body,
        img.width, img.height,
    )

    composite = Image.alpha_composite(img, overlay).convert("RGB")
    composite.save(str(out_path))
    print(f"  🖊  マーカー合成 → {out_path.name}")



# ──────────────────────────────────────────────
# メイン処理
# ──────────────────────────────────────────────
def process_group(symbol: str, date_str: str, trades: pd.DataFrame, args):
    """1銘柄×1日の処理"""
    safe_date   = date_str.replace("/", "").replace("-", "")
    out_dir     = SNAPSHOT_DIR / safe_date
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name   = f"TSE_{symbol}_1m_{safe_date}"
    raw_path    = out_dir / f"{base_name}_raw.png"
    marked_path = out_dir / f"{base_name}.png"

    print(f"\n{'='*50}")
    print(f"🏷  銘柄: {symbol}  日付: {date_str}  約定数: {len(trades)}")

    if not take_snapshot(symbol, date_str, raw_path):
        print("  ⚠️  撮影失敗。スキップします。")
        return
    draw_markers(raw_path, trades, marked_path)

    export_prompt_and_payload(symbol, date_str, trades, marked_path, out_dir, safe_date)


def main():
    sys.stdout.reconfigure(line_buffering=True, encoding="utf-8")
    parser = argparse.ArgumentParser(description="TradingView一括撮影 + プロンプト出力")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--csv",  help="約定照会CSVのパス（フルパス）")
    group.add_argument("--date", help="月日4桁 例: 0315 → 20260315_約定照会.csv を自動使用")
    args = parser.parse_args()

    if args.date:
        year = datetime.now().year
        args.csv = str(CSV_DIR / f"{year}{args.date}_約定照会.csv")
        print(f"[INFO] CSVパス: {args.csv}")

    print(f"📂 CSV読み込み: {args.csv}")
    df = load_trades_from_csv(args.csv)
    groups = group_by_symbol_date(df)
    print(f"✅ {len(groups)} 銘柄×日付 を処理します\n")

    date_groups: dict = defaultdict(list)

    for symbol, date_str, trades in groups:
        process_group(symbol, date_str, trades, args)

        safe_date   = date_str.replace("/", "").replace("-", "")
        out_dir     = SNAPSHOT_DIR / safe_date
        prompt_path = out_dir / f"TSE_{symbol}_1m_{safe_date}_prompt.txt"
        image_path  = out_dir / f"TSE_{symbol}_1m_{safe_date}.png"
        date_groups[safe_date].append((symbol, prompt_path, image_path, trades))

    for safe_date, group_data in date_groups.items():
        out_dir = SNAPSHOT_DIR / safe_date

        # ── 日経平均を日付ごとに1回撮影 ──
        nikkei_path = out_dir / f"NI225_1m_{safe_date}.png"
        if not nikkei_path.exists():
            print(f"\n{'='*50}")
            print(f"📷 日経平均（TVC:NI225）撮影中...")
            take_nikkei_snapshot(safe_date, nikkei_path)
        else:
            print(f"\n✅ 日経平均画像は既存のためスキップ: {nikkei_path.name}")

        export_summary_prompt(group_data, safe_date, out_dir)

    print(f"\n🎉 完了！保存先: {SNAPSHOT_DIR}")


if __name__ == "__main__":
    main()