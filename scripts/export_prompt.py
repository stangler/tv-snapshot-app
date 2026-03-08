"""
export_prompt.py
----------------
チャート分析用のプロンプトと画像パスをファイルに書き出す。
API呼び出しは行わない。出力ファイルを任意のエージェントに渡して使う。

使い方:
  python3 /workspace/scripts/export_prompt.py --csv /workspace/csv/20260306_約定照会.csv

出力（snapshotsの各銘柄フォルダに生成）:
  TSE_5016_1m_20260306_prompt.txt   ← エージェントに渡すプロンプト全文
  TSE_5016_1m_20260306_payload.json ← 画像パス・プロンプト・約定データをまとめたJSON
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

SNAPSHOT_DIR = Path("/workspace/snapshots")


# ──────────────────────────────────────────────
# プロンプト定義
# ──────────────────────────────────────────────
SYSTEM_PROMPT = """\
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

USER_PROMPT_TEMPLATE = """\
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
# データ整形ヘルパー
# ──────────────────────────────────────────────
def build_trade_table(trades: pd.DataFrame) -> str:
    lines = [f"{'No':>3}  {'取引':12}  {'売買':6}  {'約定単価':>12}  {'数量':>6}"]
    lines.append("-" * 52)
    for i, row in trades.iterrows():
        side    = str(row.get("side",    "-"))
        price   = row.get("price", 0)
        qty     = str(row.get("qty",     "-"))
        buysell = str(row.get("buysell", "-")) if "buysell" in row.index else ""
        display_side = f"{side} {buysell}".strip()
        lines.append(f"{i+1:>3}  {display_side:12}  {qty:>6}株  ¥{float(price):>12,.1f}")
    return "\n".join(lines)


def estimate_pnl(trades: pd.DataFrame) -> str:
    """
    ロング（買建→売埋）とショート（売建→買埋）を別スタックで管理し、
    FIFO で概算実現損益を計算する。

    ※ CSVの「取引」カラム（side）には "信用新規"/"信用返済" が入り、
       「売買」カラム（buysell）には "買建"/"売埋"/"売建"/"買埋" が入るため、
       両カラムを結合して判定する。
    """
    long_stack  = []  # (price, qty) 買建の未決済分
    short_stack = []  # (price, qty) 売建の未決済分
    realized = 0.0

    for _, row in trades.iterrows():
        # ── 修正箇所: side と buysell を結合して判定 ──
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

        is_long_entry  = "買建" in side
        is_long_exit   = "売埋" in side
        is_short_entry = "売建" in side
        is_short_exit  = "買埋" in side

        if is_long_entry:
            long_stack.append((price, qty))

        elif is_long_exit:
            remaining = qty
            while remaining > 0 and long_stack:
                bp, bq = long_stack.pop(0)
                matched = min(remaining, bq)
                realized += (price - bp) * matched
                remaining -= matched
                if bq > matched:
                    long_stack.insert(0, (bp, bq - matched))

        elif is_short_entry:
            short_stack.append((price, qty))

        elif is_short_exit:
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


def load_trades(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, encoding="utf-8-sig")
    df.columns = df.columns.str.strip()

    rename_map = {}
    col_lower  = {c.lower(): c for c in df.columns}

    # ── カラムマッピング ──
    # side    → 「取引」カラム: "信用新規" / "信用返済"
    # buysell → 「売買」カラム: "買建" / "売埋" / "売建" / "買埋"
    candidates = {
        "symbol":      ["銘柄コード", "コード", "symbol", "code"],
        "symbol_name": ["銘柄名", "name"],
        "date":        ["約定日", "date", "日付"],
        "time":        ["約定時刻", "約定時間", "time", "時刻"],
        "price":       ["約定単価(円)", "約定単価", "価格", "price", "単価"],
        "qty":         ["約定数量(株/口)", "約定数量", "数量", "qty", "quantity"],
        "side":        ["取引", "side"],          # "信用新規"/"信用返済"
        "buysell":     ["売買", "buysell"],        # "買建"/"売埋"/"売建"/"買埋"
    }
    for target, keys in candidates.items():
        for k in keys:
            if k in col_lower:
                rename_map[col_lower[k]] = target
                break

    df = df.rename(columns=rename_map)
    df["price"] = pd.to_numeric(
        df["price"].astype(str).str.replace(",", "", regex=False), errors="coerce"
    )
    df = df.dropna(subset=["price"])
    df["symbol"] = df["symbol"].astype(str).str.strip()
    df["date"]   = df["date"].astype(str).str.extract(r"(\d{4}[/\-]\d{2}[/\-]\d{2})")[0]
    return df


# ──────────────────────────────────────────────
# メイン
# ──────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="プロンプト＆データのエクスポート")
    parser.add_argument("--csv", required=True, help="約定照会CSVのパス")
    args = parser.parse_args()

    df     = load_trades(args.csv)
    groups = list(df.groupby(["symbol", "date"]))
    print(f"✅ {len(groups)} 銘柄×日付 を処理します\n")

    for (symbol, date_str), trades in groups:
        trades      = trades.reset_index(drop=True)
        symbol_name = str(trades["symbol_name"].iloc[0]) \
            if "symbol_name" in trades.columns else symbol

        safe_date  = date_str.replace("/", "").replace("-", "")
        out_dir    = SNAPSHOT_DIR / safe_date
        image_path = out_dir / f"TSE_{symbol}_1m_{safe_date}.png"

        if not image_path.exists():
            print(f"⚠️  画像なし: {image_path}  （先に撮影してください）")
            image_note = "※ 画像未生成。batch_snapshot.py で撮影後に再実行してください。"
        else:
            image_note = str(image_path)

        trade_table = build_trade_table(trades)
        pnl_text    = estimate_pnl(trades)

        user_prompt = USER_PROMPT_TEMPLATE.format(
            date        = date_str,
            symbol      = symbol,
            symbol_name = symbol_name,
            trade_table = trade_table,
            pnl_text    = pnl_text,
            image_path  = image_note,
        )

        # ── プロンプトテキストを書き出す ──
        prompt_path = out_dir / f"TSE_{symbol}_1m_{safe_date}_prompt.txt"
        out_dir.mkdir(parents=True, exist_ok=True)
        with open(prompt_path, "w", encoding="utf-8") as f:
            f.write("=== SYSTEM PROMPT ===\n")
            f.write(SYSTEM_PROMPT)
            f.write("\n\n=== USER PROMPT ===\n")
            f.write(user_prompt)
        print(f"📄 プロンプト出力 → {prompt_path.name}")

        # ── JSON（構造化データ）を書き出す ──
        payload = {
            "generated_at":  datetime.now().isoformat(),
            "symbol":        symbol,
            "symbol_name":   symbol_name,
            "date":          date_str,
            "image_path":    str(image_path),
            "system_prompt": SYSTEM_PROMPT,
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
        print(f"📦 JSONペイロード出力 → {json_path.name}")

    print("\n🎉 完了！")


if __name__ == "__main__":
    main()