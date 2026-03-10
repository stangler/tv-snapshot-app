"""
test_batch_snapshot.py
----------------------
batch_snapshot.py のユニットテスト

実行方法（コンテナ内）:
  pip install pytest Pillow --break-system-packages
  pytest /workspace/scripts/test_batch_snapshot.py -v
"""

import io
import textwrap
from pathlib import Path

import pandas as pd
import pytest
from PIL import Image

# ── テスト対象のインポート ──────────────────────────────────────
import sys
sys.path.insert(0, "/workspace/scripts")
from batch_snapshot import (
    build_trade_table,
    draw_markers,
    estimate_pnl,
    group_by_symbol_date,
    load_trades_from_csv,
    price_to_y,
    CHART_TOP_PX,
    CHART_BOTTOM_PX,
)


# ══════════════════════════════════════════════════════════════
# ヘルパー
# ══════════════════════════════════════════════════════════════

def make_trades(*rows) -> pd.DataFrame:
    """
    テスト用の約定DataFrameを簡単に作る。
    rows: (side, buysell, price, qty) のタプルのリスト
    """
    return pd.DataFrame(
        [{"side": s, "buysell": b, "price": p, "qty": q} for s, b, p, q in rows]
    )


def make_csv(content: str, tmp_path: Path, filename="test.csv") -> Path:
    """CSV文字列をファイルに書き出してパスを返す"""
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8-sig")
    return p


def make_image(width=1920, height=1080) -> Image.Image:
    """ダミーのチャート画像（黒背景）"""
    return Image.new("RGB", (width, height), color=(30, 30, 30))


# ══════════════════════════════════════════════════════════════
# 1. CSV パース
# ══════════════════════════════════════════════════════════════

class TestLoadTradesFromCsv:

    def test_standard_columns(self, tmp_path):
        """標準的なカラム名で正しく読み込めること"""
        csv = make_csv("""\
            コード,約定日,取引,売買,約定単価(円),約定数量(株/口)
            5016,2026/03/06,信用新規,買建,"3,969.0",200
            5016,2026/03/06,信用返済,売埋,"4,010.0",200
        """, tmp_path)
        df = load_trades_from_csv(str(csv))
        assert len(df) == 2
        assert list(df["symbol"]) == ["5016", "5016"]
        assert df["price"].iloc[0] == pytest.approx(3969.0)
        assert df["price"].iloc[1] == pytest.approx(4010.0)

    def test_date_with_time_is_stripped(self, tmp_path):
        """約定日に時刻が含まれていても日付部分だけ抽出されること"""
        csv = make_csv("""\
            コード,約定日,取引,売買,約定単価(円),約定数量(株/口)
            5016,2026/03/06 09:03:40,信用新規,買建,3969.0,200
        """, tmp_path)
        df = load_trades_from_csv(str(csv))
        assert df["date"].iloc[0] == "2026/03/06"

    def test_price_with_comma(self, tmp_path):
        """カンマ区切り価格が正しく数値変換されること"""
        csv = make_csv("""\
            コード,約定日,取引,売買,約定単価(円),約定数量(株/口)
            5016,2026/03/06,信用新規,買建,"4,033.6",200
        """, tmp_path)
        df = load_trades_from_csv(str(csv))
        assert df["price"].iloc[0] == pytest.approx(4033.6)

    def test_invalid_price_rows_dropped(self, tmp_path):
        """価格が無効な行はDropされること"""
        csv = make_csv("""\
            コード,約定日,取引,売買,約定単価(円),約定数量(株/口)
            5016,2026/03/06,信用新規,買建,invalid,200
            5016,2026/03/06,信用返済,売埋,4010.0,200
        """, tmp_path)
        df = load_trades_from_csv(str(csv))
        assert len(df) == 1
        assert df["price"].iloc[0] == pytest.approx(4010.0)

    def test_missing_required_column_raises(self, tmp_path):
        """必須カラム（symbol/date/price）がない場合はValueErrorを送出すること"""
        csv = make_csv("""\
            約定日,取引,売買,約定単価(円)
            2026/03/06,信用新規,買建,3969.0
        """, tmp_path)
        with pytest.raises(ValueError, match="CSVに必要なカラムが見つかりません"):
            load_trades_from_csv(str(csv))

    def test_group_by_symbol_date(self, tmp_path):
        """銘柄×日付でグループ化されること"""
        csv = make_csv("""\
            コード,約定日,取引,売買,約定単価(円),約定数量(株/口)
            5016,2026/03/06,信用新規,買建,3969.0,200
            5016,2026/03/06,信用返済,売埋,4010.0,200
            7203,2026/03/06,信用新規,買建,2800.0,100
        """, tmp_path)
        df = load_trades_from_csv(str(csv))
        groups = group_by_symbol_date(df)
        assert len(groups) == 2
        symbols = {g[0] for g in groups}
        assert symbols == {"5016", "7203"}


# ══════════════════════════════════════════════════════════════
# 2. 損益計算
# ══════════════════════════════════════════════════════════════

class TestEstimatePnl:

    def test_long_profit(self):
        """ロングで利確できること"""
        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
            ("信用返済", "売埋", 4000, 200),
        )
        result = estimate_pnl(trades)
        assert "+20,000円" in result

    def test_long_loss(self):
        """ロングで損切りできること"""
        trades = make_trades(
            ("信用新規", "買建", 4000, 200),
            ("信用返済", "売埋", 3900, 200),
        )
        result = estimate_pnl(trades)
        assert "-20,000円" in result

    def test_short_profit(self):
        """ショートで利確できること"""
        trades = make_trades(
            ("信用新規", "売建", 4000, 200),
            ("信用返済", "買埋", 3900, 200),
        )
        result = estimate_pnl(trades)
        assert "+20,000円" in result

    def test_short_loss(self):
        """ショートで損切りできること"""
        trades = make_trades(
            ("信用新規", "売建", 3900, 200),
            ("信用返済", "買埋", 4000, 200),
        )
        result = estimate_pnl(trades)
        assert "-20,000円" in result

    def test_fifo_order(self):
        """FIFOで先に建てたポジションから決済されること"""
        trades = make_trades(
            ("信用新規", "買建", 3900, 200),   # ← FIFOでこちらが先に決済される
            ("信用新規", "買建", 3950, 200),
            ("信用返済", "売埋", 4000, 200),
        )
        result = estimate_pnl(trades)
        # 3900円建て200株が決済: (4000-3900)*200 = +20,000円
        assert "+20,000円" in result
        # 3950円建て200株は未決済
        assert "未決済ロング建玉" in result

    def test_open_position_reported(self):
        """未決済建玉が出力に含まれること"""
        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
        )
        result = estimate_pnl(trades)
        assert "未決済ロング建玉" in result
        assert "3,900.0" in result

    def test_zero_pnl_breakeven(self):
        """同値決済は±0円になること"""
        trades = make_trades(
            ("信用新規", "買建", 4000, 100),
            ("信用返済", "売埋", 4000, 100),
        )
        result = estimate_pnl(trades)
        assert "+0円" in result

    def test_multiple_lots_mixed(self):
        """複数回転トレードの合算損益が正しいこと"""
        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
            ("信用返済", "売埋", 4000, 200),   # +20,000
            ("信用新規", "買建", 4050, 200),
            ("信用返済", "売埋", 4020, 200),   # -6,000
        )
        result = estimate_pnl(trades)
        assert "+14,000円" in result


# ══════════════════════════════════════════════════════════════
# 3. 約定テーブル生成
# ══════════════════════════════════════════════════════════════

class TestBuildTradeTable:

    def test_contains_header(self):
        """ヘッダー行が含まれること"""
        trades = make_trades(("信用新規", "買建", 3900, 200))
        table = build_trade_table(trades)
        assert "No" in table
        assert "損益" in table

    def test_buy_row_shows_dash(self):
        """建玉行（買建）の損益は"-"であること"""
        trades = make_trades(("信用新規", "買建", 3900, 200))
        table = build_trade_table(trades)
        assert "買建" in table
        # 損益列はダッシュ
        lines = [l for l in table.splitlines() if "買建" in l]
        assert lines and lines[0].strip().endswith("-")

    def test_sell_close_shows_pnl(self):
        """決済行に損益と✅/❌が表示されること"""
        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
            ("信用返済", "売埋", 4000, 200),
        )
        table = build_trade_table(trades)
        assert "✅" in table
        assert "20,000円" in table

    def test_loss_shows_cross(self):
        """損失の場合❌が表示されること"""
        trades = make_trades(
            ("信用新規", "買建", 4000, 200),
            ("信用返済", "売埋", 3900, 200),
        )
        table = build_trade_table(trades)
        assert "❌" in table

    def test_row_count_matches(self):
        """ヘッダー+区切り+データ行の数が約定数と一致すること"""
        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
            ("信用返済", "売埋", 4000, 200),
            ("信用新規", "買建", 4050, 200),
        )
        table = build_trade_table(trades)
        data_lines = [
            l for l in table.splitlines()
            if l.strip() and not l.strip().startswith("No") and not l.strip().startswith("-")
        ]
        assert len(data_lines) == 3


# ══════════════════════════════════════════════════════════════
# 4. 価格→Y座標変換
# ══════════════════════════════════════════════════════════════

class TestPriceToY:

    def test_max_price_maps_to_top(self):
        """最高値はチャート上端に近いY座標になること"""
        y = price_to_y(4000, 3900, 4000)
        assert y == CHART_TOP_PX

    def test_min_price_maps_to_bottom(self):
        """最安値はチャート下端に近いY座標になること"""
        y = price_to_y(3900, 3900, 4000)
        assert y == CHART_BOTTOM_PX

    def test_mid_price_maps_to_center(self):
        """中間価格はチャート中央付近になること"""
        center = (CHART_TOP_PX + CHART_BOTTOM_PX) // 2
        y = price_to_y(3950, 3900, 4000)
        assert abs(y - center) <= 2

    def test_equal_price_range_returns_center(self):
        """価格レンジが0の場合は中央を返すこと"""
        center = (CHART_TOP_PX + CHART_BOTTOM_PX) // 2
        y = price_to_y(4000, 4000, 4000)
        assert y == center


# ══════════════════════════════════════════════════════════════
# 5. マーカー描画
# ══════════════════════════════════════════════════════════════

class TestDrawMarkers:

    def test_output_file_created(self, tmp_path):
        """マーカー合成後のファイルが生成されること"""
        img = make_image()
        src = tmp_path / "raw.png"
        dst = tmp_path / "marked.png"
        img.save(str(src))

        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
            ("信用返済", "売埋", 4000, 200),
        )
        draw_markers(src, trades, dst)
        assert dst.exists()

    def test_output_is_valid_image(self, tmp_path):
        """生成ファイルが有効な画像として開けること"""
        img = make_image()
        src = tmp_path / "raw.png"
        dst = tmp_path / "marked.png"
        img.save(str(src))

        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
        )
        draw_markers(src, trades, dst)
        result = Image.open(dst)
        assert result.size == (1920, 1080)

    def test_marker_changes_pixels(self, tmp_path):
        """マーカーが描画されて元画像から変化していること"""
        img = make_image()
        src = tmp_path / "raw.png"
        dst = tmp_path / "marked.png"
        img.save(str(src))

        trades = make_trades(
            ("信用新規", "買建", 3900, 200),
            ("信用返済", "売埋", 4000, 200),
        )
        draw_markers(src, trades, dst)

        orig = list(Image.open(src).getdata())
        marked = list(Image.open(dst).getdata())
        assert orig != marked

    def test_empty_trades_does_not_crash(self, tmp_path):
        """約定データが空でもエラーにならないこと"""
        img = make_image()
        src = tmp_path / "raw.png"
        dst = tmp_path / "marked.png"
        img.save(str(src))

        trades = pd.DataFrame(columns=["side", "buysell", "price", "qty"])
        draw_markers(src, trades, dst)
        assert dst.exists()

    def test_nan_price_rows_skipped(self, tmp_path):
        """価格がNaNの行はスキップされてエラーにならないこと"""
        img = make_image()
        src = tmp_path / "raw.png"
        dst = tmp_path / "marked.png"
        img.save(str(src))

        trades = pd.DataFrame([
            {"side": "信用新規", "buysell": "買建", "price": float("nan"), "qty": 200},
            {"side": "信用返済", "buysell": "売埋", "price": 4000.0, "qty": 200},
        ])
        draw_markers(src, trades, dst)
        assert dst.exists()