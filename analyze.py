"""
米国株指標チェック 解析エンジン（GitHub Actionsで fetch_data.py の後に実行）

入力（--out フォルダ内、fetch_data.py の出力）:
  prices_daily.csv.gz   監視母集団（テック・半導体＋指数・ETF）の日足 約3年
  prices_sp500.csv.gz   S&P500構成銘柄の日足 約2年（市場全体のRS・先導株・ブレイク統計用）
  sp500_constituents.csv  S&P500構成銘柄とGICSセクター（先導セクターの集計に使う）
  breadth.csv           市場内部（proxy）
  events.csv            決算予定日（取得できた分）
  manifest.json
  prev/ledger.json      前回の台帳（workflowが前回のdataブランチから持ってくる。無ければ初回）
  prev/screen.csv       前回のスクリーン（--prev-screen。セクター区分の flip など差分イベント用）
  prev/history_signals.csv  前回までの判定履歴（--prev-history。無ければ main の history_seed.csv）

出力（--out フォルダ）:
  signals.json   日次・週次レポートが読む要約（市場、Tapeチェックリスト、先導株、セットアップ、当日の重要イベント）
  screen.csv     母集団＋S&P500の全銘柄スクリーン（TT、RS順位、位置）
  setups.csv     テック・半導体のセットアップ一覧
  ledger.json    台帳（セットアップ登録、ブレイク追跡、レジーム履歴）。次回に引き継ぐ
  history_signals.csv  判定結果の日次履歴（1日1行。F2.2）

定義はマスター仕様 v4.2 の C7.1 / C15.1 / E4〜E14 に対応する。閾値はすべて P（プロジェクト規則）。
v4.3 追補（03_Addendum_v4.3_SectorRS_Resilience.txt）：E7.1 セクター相対強度、E7.2 調整耐性を追加。
  screen.csv の列、signals.json の sector_rs / correction_resilience、key_events の
  sector_rs_flip / next_candidate_new / next_candidate_out / late_break_new（前回の screen.csv との差分。--prev-screen）。
v4.3b：RSライン（E7。対SPY、補助で対QQQ）の状態を追加。signals.json の rs_lines、screen.csv の rs_line_* 列、
  key_events の rs_line_lead（RS新高値が株価に先行）／rs_line_new_high（株価とそろった新高値。前回から20営業日以上あいた場合）。
v4.4 追補（04_Addendum_v4.4_Breadth_FollowThrough.txt）：
  (1) FB1の「伸び」：維持率が高くてもD+5・D+10の平均リターンがともにマイナスなら「伸びない」（FB1_stalling）とし、
      E・Aの支持に数えず、Cの支持に数える。
  (2) 市場内部の拡張 BR4・BR5：52週高値から−20%以上の銘柄比率と50日線上比率を、S&P500全体・IT＋通信・それ以外で計算。
      隠れ弱気（ステルス・ベア）のラベル、大型株主導の下落（megacap_led）。signals.json の breadth_ext。
  (3) 指数の崩れガード（IX3）：SPYかQQQが50日線の下、かつ有効分配日5以上なら、内部回復入口のEにしない（高値圏ならC）。
  key_events の breadth_label_change／index_breakdown_guard／breakout_stalling（前営業日との差分）。
v4.5 追補（06_Addendum_v4.5_Leadership_Hedge_SPYModel.txt）。いずれも観察専用で、Tapeレジーム・買い判定は変えない：
  (A) 先導セクターの推移 E7.4：signals.json の leadership（セクター別・監視グループ別の先導株数 今／4週前、新規・脱落、セクターETFの順位）。
      key_events の leadership_shift。
  (B) ヘッジ・指数ショートの条件 C10.1：signals.json の hedge（許可、今日の価格セットアップ、損切り、解除）。key_events の hedge_setup／hedge_cover。
  (C) 長期SPY配分モデル F10：signals.json の spy_model（5項目、配分%、buy／sell）。key_events の spy_model_signal。
v4.5b（F2.2）：判定結果の日次履歴 history_signals.csv（1日1行。レジーム、チェックリスト、先導セクター、ヘッジ、長期モデル、候補数、イベント種類）を
  data ブランチに追記する（--prev-history で前回分を引き継ぎ、無ければ main の history_seed.csv＝backfill から始める）。
v4.6 追補（07_Addendum_v4.6_Setups_PocketPivot_Stage.txt）。Tape・Broad・L/H・VCP等の判定は変えない：
  E6.2 週足ステージ（v4.6b：状態遷移、細区分1A〜4B-、支え・抵抗、マンスフィールドRS）、E6.3 業種別のステージ分布、
  E9.5a ベースカウント、E9.2a カップウィズハンドル、E9.6 フラットベース、
  E9.7 ダブルボトム、E10.1a ポケットピボット（10日線か50日線の近く、V字・ウェッジ除外、種類、台帳でD+3/5/10を追跡）、
  E13.2 利益確定の目安（+20〜25%）と8週ルール、E14.1a setup_type。
  signals.json の stages／setup_counts／pocket_pivots／profit_tracking、setups の stage・base_count・setup_type、
  screen.csv の stage 等の列、key_events の pocket_pivot／profit_zone／eight_week_hold／eight_week_end／stage_change／sector_stage_shift。
"""
import argparse
import json
import math
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

DEF_VERSION = "ST-1.5b / engine 1.4b"
JST = timezone(timedelta(hours=9))
INDEX_TICKERS = {"^GSPC", "^IXIC", "^VIX"}
SECTOR_ETFS = ["XLK", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLU"]
TECH_ETFS = ["SOXX", "SMH", "XLK", "VGT", "IGV", "CIBR", "SKYY"]
BENCH = "SPY"

# E7.1（v4.3）: 監視グループ → (基準ETF, 確認用ETF)。tickers.txt の見出しのキーワードで照合する
GROUP_BENCH = {"半導体": ("SOXX", "SMH"), "ソフトウェア": ("IGV", "XLK"), "ハードウェア": ("XLK", "VGT")}
# E7.1（v4.3）: S&P500構成銘柄（監視母集団外）の GICS セクター → セクターSPDR
GICS_BENCH = {"Information Technology": "XLK", "Communication Services": "XLC", "Consumer Discretionary": "XLY",
              "Consumer Staples": "XLP", "Energy": "XLE", "Financials": "XLF", "Health Care": "XLV",
              "Industrials": "XLI", "Materials": "XLB", "Real Estate": "XLRE", "Utilities": "XLU"}
# E7.2（v4.3）: 調整窓の閾値（P）
RES = {"min_depth": -5.0, "shallow": -8.0, "min_bars": 5, "keep_bars": 60, "min_history": 120,
       "held_max": 0.5, "inline_max": 1.0, "now_na_above": -2.0, "newhigh_upgrade_dd": -0.15}
# v4.4（04追補）: 市場内部の拡張・ブレイクの伸び・指数の崩れガードの閾値（すべてP）
BRX = {"down_pct": 20.0,        # 52週高値（日中高値）から何%下を「−20%以上」とするか
       "watch": 40.0,           # −20%以上の銘柄比率がこれ以上 → 警戒
       "stealth": 50.0,         # 同じく過半 → 隠れ弱気（指数が高値圏のとき）／全面下落（指数も−10%超のとき）
       "near_high": -10.0,      # 指数・基準ETFが52週高値からこの%以内なら「高値圏」（C15.1のCと同じ）
       "min_cov": 0.8,          # グループの8割以上に52週高値・50日線の値がそろわなければ計算しない
       "megacap_qqq_rel_1m": -3.0,   # QQQのSPY比1か月がこれ以下
       "megacap_rsp_spy_4w": 2.0,    # かつRSP/SPYの4週変化がこれ以上 → 大型株主導の下落
       "guard_dist_min": 5,     # 指数の崩れガード：有効分配日数の下限
       "fb_min_matured": 20}    # FB1の伸び判定に使う最小標本（D+5成熟件数）
TECH_SECTORS = ("Information Technology", "Communication Services")
BX_LABEL_JA = {"stealth": "隠れ弱気（指数は高値圏なのに過半が−20%以上）", "watch": "隠れ弱気の警戒（−20%以上が4割超）",
               "broad_decline": "全面的な下落（指数・基準ETFも−10%超）"}
# v4.6（07追補）: ステージ・ベースカウント・カップ・フラットベース・ダブルボトム・ポケットピボット・利益確定の目安（すべてP）
STG = {"flat": 1.0, "near_ma": 8.0, "bo_min_slope": 0.5, "s4_to1_slope": 2.0, "mrs_weeks": 52,   # E6.2（07追補）：週足ステージの閾値（P）
       "support_weeks": 13, "break_below_ma": 5.0, "base_fail_pct": 3.0, "s4_min_weeks": 3,
       "a_weeks": 8, "a_max_ext": 25.0, "b_age": 30, "b_ext": 40.0, "b_div_weeks": 8,
       "s1_early_weeks": 6, "s1_late_weeks": 6, "s1b_near_res": 10.0, "s3_early_weeks": 5, "s3_late_weeks": 8, "s3_near_support": 5.0,
       "s4_early_weeks": 6, "s4_late_weeks": 10, "s4_nolow_late": 3, "s4_nolow_bminus": 6, "s4_bminus_near_ma": 5.0}
BC = {"min_depth": 0.08, "max_depth": 0.50, "min_days": 25}           # E9.5a：ベース＝5週以上、深さ8〜50%
READY = {"near_pivot": 0.92, "extended": 1.05, "max_risk": 0.07, "watch_risk": 0.08}  # VCPと同じ（E9.1・E11）
CUP = {"min_depth": 0.15, "max_depth": 0.30, "min_total_days": 35, "max_total_days": 325, "prior_up": 0.30,
       "side_min_days": 10, "handle_min": 5, "handle_max": 20, "handle_max_depth": 0.12, "handle_hi_vs_left": 0.85,
       "right_max_vs_left": 1.02, "handle_vol_max": 0.80}                # E9.2a（IBD教材版＝本文E9.2の値）
FLAT = {"min_days": 25, "max_days": 65, "max_depth": 0.15, "ideal_depth": 0.10, "top_band": 0.95, "flat_top": 1.03, "prior_up": 0.20,
        "stop_lookback": 10, "dry5_max": 1.0}                              # E9.6
DB = {"min_days": 35, "max_days": 325, "min_depth": 0.15, "max_depth": 0.35, "prior_up": 0.30, "undercut_max": 0.05,
      "no_undercut_tol": 0.03, "mid_min_frac": 0.4, "leg_min_days": 5, "stop_lookback": 10, "low_vol_max": 1.0}  # E9.7
PPV = {"lookback": 10, "near_low_pct": 2.0, "max_above_ma_pct": 5.0, "vshape_below_pct": 5.0, "wedge_days": 15,
       "wedge_rise_pct": 5.0, "max_risk_pct": 7.0, "base_lookback": 65, "base_corr": 0.08, "dedupe_days": 5}  # E10.1a
PT = {"zone_lo": 20.0, "zone_hi": 25.0, "eight_week_gain": 20.0, "eight_week_window": 15, "eight_week_hold": 40,
      "track_days": 60}                                                    # E13.2
STANDARD_PATTERNS = ("vcp", "cup_handle", "flat_base", "double_bottom")
PATTERN_JA = {"vcp": "VCP", "cup_handle": "カップウィズハンドル", "flat_base": "フラットベース", "double_bottom": "ダブルボトム",
              "power_play": "パワープレイ", "cheat": "チート", "low_cheat": "ロー・チート", "pocket_pivot": "ポケットピボット"}


# ----------------------------------------------------------------------------
# 読込
# ----------------------------------------------------------------------------
def load_prices(path):
    if not os.path.exists(path):
        return None
    df = pd.read_csv(path)
    df["date"] = pd.to_datetime(df["date"])
    return df


def parse_universe_groups(tickers_txt):
    """tickers.txt の見出しコメントから、銘柄→区分（個別分析の対象か、業種）を作る。"""
    groups, current = {}, "other"
    if not os.path.exists(tickers_txt):
        return groups
    for line in open(tickers_txt, encoding="utf-8"):
        s = line.strip()
        m = re.match(r"#\s*---\s*(.+?)\s*---", s)
        if m:
            current = m.group(1)
            continue
        t = s.split("#")[0].strip().upper()
        if t:
            groups[t] = current
    return groups


def is_stock_group(g):
    """個別株の区分か（ETF・指数の区分は除く）。"""
    if "ETF" in g or "指数" in g or "市場全体" in g:
        return False
    return any(k in g for k in ["半導体", "ソフトウェア", "ハードウェア"])


def load_sp500_sectors(out):
    """fetch_data.py が保存した構成銘柄一覧（ticker, sector）。無ければ空。"""
    p = os.path.join(out, "sp500_constituents.csv")
    if not os.path.exists(p):
        return {}
    df = pd.read_csv(p)
    return dict(zip(df["ticker"].astype(str).str.upper(), df["sector"].astype(str)))


def wide(df, col):
    return df.pivot(index="date", columns="ticker", values=col).sort_index()


# ----------------------------------------------------------------------------
# 指標（E4）
# ----------------------------------------------------------------------------
def ema(s, n):
    return s.ewm(span=n, adjust=False, min_periods=n).mean()


class Panel:
    """全銘柄の指標をまとめて持つ。"""

    def __init__(self, px):
        self.C, self.H, self.L, self.V = wide(px, "close"), wide(px, "high"), wide(px, "low"), wide(px, "volume")
        self.O = wide(px, "open")
        self.dates = self.C.index
        C = self.C
        self.s10, self.s50, self.s150, self.s200 = C.rolling(10).mean(), C.rolling(50).mean(), C.rolling(150).mean(), C.rolling(200).mean()
        self.e21 = C.apply(lambda s: ema(s, 21))
        self.h252, self.l252 = self.H.rolling(252).max(), self.L.rolling(252).min()
        self.h252c = C.rolling(252, min_periods=RES["min_history"]).max()  # 終値ベースの52週高値（E7.2）
        self.vavg50_prev = self.V.rolling(50).mean().shift(1)
        self.rvol50 = self.V / self.vavg50_prev
        self.dry5 = self.V.rolling(5).median() / self.V.rolling(50).mean().shift(5)
        self.ret1 = C.pct_change()

    def tt7(self, i=None):
        """Trend Template 価格7項目（E5）。iは日付位置（省略時は最終日）。"""
        i = len(self.dates) - 1 if i is None else i
        C, s50, s150, s200 = self.C.iloc[i], self.s50.iloc[i], self.s150.iloc[i], self.s200.iloc[i]
        s200_prev = self.s200.iloc[i - 21]
        seg = self.s200.iloc[i - 21:i + 1]
        x = np.arange(len(seg))
        slope = seg.apply(lambda col: np.polyfit(x, col.values, 1)[0] if col.notna().all() else np.nan)
        items = pd.DataFrame({
            "tt1": (C > s150) & (C > s200),
            "tt2": s150 > s200,
            "tt3": (s200 > s200_prev) & (slope > 0),
            "tt4": (s50 > s150) & (s50 > s200),
            "tt5": C > s50,
            "tt6": C >= 1.30 * self.l252.iloc[i],
            "tt7": C >= 0.75 * self.h252.iloc[i],
        })
        valid = pd.concat([C, s200, s200_prev, self.l252.iloc[i], self.h252.iloc[i]], axis=1).notna().all(axis=1)
        items["tt_count"] = items[[f"tt{k}" for k in range(1, 8)]].sum(axis=1).where(valid)
        items["tt7_pass"] = items["tt_count"] == 7
        # 5か月確認（P）
        ok5 = pd.Series(True, index=C.index)
        for k in (21, 42, 63, 84, 105):
            ok5 &= s200 > self.s200.iloc[i - k]
        items["tt3_5m"] = ok5.where(valid)
        return items

    def rs(self, i=None):
        """E7: SPY比の相対モメンタムと母集団内百分位。"""
        i = len(self.dates) - 1 if i is None else i
        C = self.C
        B = C[BENCH]
        out = {}
        for k in (21, 63, 126, 252):
            out[f"rel_{k}"] = (C.iloc[i] / C.iloc[i - k]) / (B.iloc[i] / B.iloc[i - k]) - 1
        df = pd.DataFrame(out)
        df["rs_m"] = 0.4 * np.log1p(df["rel_63"]) + 0.3 * np.log1p(df["rel_126"]) + 0.3 * np.log1p(df["rel_252"])
        return df


# ----------------------------------------------------------------------------
# 市場: 分配日・ストーリング・FTD（C7.1）
# ----------------------------------------------------------------------------
def distribution_state(C, V, H, L, i):
    """指数1本について、i時点の有効分配日・ストーリング・ラリー/FTD状態を返す。"""
    c, v = C.iloc[: i + 1], V.iloc[: i + 1]
    chg = c.pct_change()
    hi252 = c.rolling(252).max()
    dist, stall = [], []
    for j in range(max(1, len(c) - 25), len(c)):
        if chg.iloc[j] <= -0.002 and v.iloc[j] > v.iloc[j - 1]:
            if not (c.iloc[j + 1:] >= c.iloc[j] * 1.05).any():
                dist.append(str(c.index[j].date()))
        rng = H.iloc[j] - L.iloc[j]
        if 0 <= chg.iloc[j] < 0.004 and v.iloc[j] > v.iloc[j - 1] and rng > 0 and (c.iloc[j] - L.iloc[j]) / rng < 0.5 and c.iloc[j] >= 0.95 * hi252.iloc[j]:
            stall.append(str(c.index[j].date()))
    # ラリーアテンプト / FTD（直近60日を走査）
    rally_day1, ftd, ftd_failed, rally_low = None, None, False, None
    lo = None
    start = max(1, len(c) - 60)
    in_corr = False
    for j in range(start, len(c)):
        if c.iloc[j] <= 0.95 * hi252.iloc[j]:
            in_corr = True
        if in_corr:
            if lo is None or c.iloc[j] <= lo:
                lo = c.iloc[j]
                rally_day1, ftd, ftd_failed, rally_low = None, None, False, None
            elif rally_day1 is None and chg.iloc[j] > 0:
                rally_day1 = j
                rally_low = L.iloc[j]
            elif rally_day1 is not None:
                n = j - rally_day1 + 1
                if ftd is None and 4 <= n <= 25 and chg.iloc[j] >= 0.0125 and v.iloc[j] > v.iloc[j - 1]:
                    ftd = j
                if c.iloc[j] < rally_low:
                    ftd_failed = ftd is not None
                    rally_day1, ftd, rally_low = None, None, None
                    lo = c.iloc[j]
    return {
        "distribution_days": dist,
        "distribution_count": len(dist),
        "stalling_days": stall,
        "rally_attempt_day1": str(c.index[rally_day1].date()) if rally_day1 is not None else None,
        "ftd_date": str(c.index[ftd].date()) if ftd is not None else None,
        "ftd_days_ago": (len(c) - 1 - ftd) if ftd is not None else None,
        "ftd_failed": ftd_failed,
        # v4.6d: ほかの pct_from_52w_high と同じ「%・小数1桁」に統一（以前は小数の比率だった）
        "pct_from_52w_high": round(100 * float(c.iloc[-1] / hi252.iloc[-1] - 1), 1) if not math.isnan(hi252.iloc[-1]) else None,
    }


# ----------------------------------------------------------------------------
# 市場内部（breadth.csv）
# ----------------------------------------------------------------------------
def load_breadth(out):
    p = os.path.join(out, "breadth.csv")
    if not os.path.exists(p):
        return None
    b = pd.read_csv(p)
    b["date"] = pd.to_datetime(b["date"])
    w = b.pivot(index="date", columns="series", values="value").sort_index()
    return w


def breadth_items(bw, asof):
    if bw is None or len(bw) < 25:
        return {}
    bw = bw[bw.index <= asof]
    if bw.empty:
        return {}
    nhnl = (bw["sp500_new_52w_highs"] - bw["sp500_new_52w_lows"]).rolling(5).mean()
    g = lambda s, k: float(s.iloc[-1 - k]) if len(s) > k and not math.isnan(s.iloc[-1 - k]) else None
    return {
        "BR1_pct_above_50sma": g(bw["sp500_pct_above_50sma"], 0),
        "BR1_4w_ago": g(bw["sp500_pct_above_50sma"], 20),
        "pct_above_200sma": g(bw["sp500_pct_above_200sma"], 0),
        "BR2_nhnl_5d": g(nhnl, 0),
        "BR2_4w_ago": g(nhnl, 20),
        "new_highs": g(bw["sp500_new_52w_highs"], 0),
        "new_lows": g(bw["sp500_new_52w_lows"], 0),
        "advancers": g(bw["sp500_advancers"], 0),
        "decliners": g(bw["sp500_decliners"], 0),
        "breadth_as_of": str(bw.index[-1].date()),
    }


# ----------------------------------------------------------------------------
# ブレイク追随率（FB1の機械的代替）
# ----------------------------------------------------------------------------
def breakout_followthrough(P, stocks, i, window=60):
    """直近window日に『終値が過去50日高値を上抜け、RVOL50>=1.5』となった事象について、
    D+5 / D+10 で終値がブレイク水準以上を維持した比率（P、機械的代替）。"""
    C, H, R = P.C[stocks], P.H[stocks], P.rvol50[stocks]
    prior_hi = C.rolling(50).max().shift(1)
    ev = []
    for j in range(max(60, i - window), i + 1):
        cond = (C.iloc[j] > prior_hi.iloc[j]) & (C.iloc[j - 1] <= prior_hi.iloc[j - 1]) & (R.iloc[j] >= 1.5)
        for t in cond[cond.fillna(False)].index:
            lvl = prior_hi.iloc[j][t]
            rec = {"ticker": t, "date": str(C.index[j].date()), "level": float(lvl)}
            for k in (3, 5, 10):
                if j + k <= i:
                    rec[f"held_d{k}"] = bool(C.iloc[j + k][t] >= lvl)
                    rec[f"ret_d{k}"] = float(C.iloc[j + k][t] / C.iloc[j][t] - 1)
            ev.append(rec)
    res = {"events": len(ev)}
    for k in (3, 5, 10):
        m = [e for e in ev if f"held_d{k}" in e]
        res[f"d{k}_matured"] = len(m)
        res[f"d{k}_hold_rate"] = round(100 * sum(e[f"held_d{k}"] for e in m) / len(m), 1) if m else None
        res[f"d{k}_avg_ret_pct"] = round(100 * float(np.mean([e[f"ret_d{k}"] for e in m])), 2) if m else None
    return res


def fb_quality(fb):
    """v4.4 (1): FB1の「伸び」。維持率だけでなく、ブレイク当日の終値からの平均リターンを見る（P）。
    stalling＝標本20件以上で、D+5とD+10の平均リターンがともにマイナス（水準は守っても伸びない）。"""
    d5r, d10r = fb.get("d5_avg_ret_pct"), fb.get("d10_avg_ret_pct")
    ok = fb.get("d5_matured", 0) >= BRX["fb_min_matured"]
    stalling = bool(ok and d5r is not None and d10r is not None and d5r < 0 and d10r < 0)
    extends = bool(ok and d5r is not None and d5r >= 0 and (d10r is None or d10r >= 0))
    return {"sample_ok": bool(ok), "extends": extends, "stalling": stalling}


# ----------------------------------------------------------------------------
# v4.4 (2): 市場内部の拡張（BR4・BR5）と隠れ弱気のラベル
# ----------------------------------------------------------------------------
def breadth_ext(P, sectors, i):
    """S&P500構成銘柄（現在の構成、proxy）を 全体・IT＋通信・それ以外 に分け、i時点の
    52週高値から−20%以上の銘柄比率と50日線上比率、4週前（i−20）の値、基準ETFの位置とラベルを返す。"""
    mem = [t for t in P.C.columns if t in sectors]
    tech = [t for t in mem if sectors[t] in TECH_SECTORS]
    rest = [t for t in mem if t not in tech]
    cut = 1 - BRX["down_pct"] / 100

    def snap(g, k):
        if not g or k < 0:
            return None, None, 0
        C, H, M = P.C[g].iloc[k], P.h252[g].iloc[k], P.s50[g].iloc[k]
        vh, vm = C.notna() & H.notna(), C.notna() & M.notna()
        need = BRX["min_cov"] * len(g)
        d20 = round(100 * float(((C <= cut * H) & vh).sum()) / float(vh.sum()), 1) if vh.sum() >= need else None
        a50 = round(100 * float(((C > M) & vm).sum()) / float(vm.sum()), 1) if vm.sum() >= need else None
        return d20, a50, int(vh.sum())

    def bench_dd(b, k):
        if b is None or b not in P.C.columns or k < 0:
            return None
        h = P.h252[b].iloc[k]
        return None if math.isnan(h) else round(100 * float(P.C[b].iloc[k] / h - 1), 1)

    def label(d20, bdd):
        if d20 is None or bdd is None:
            return None
        if d20 >= BRX["stealth"]:
            return "stealth" if bdd >= BRX["near_high"] else "broad_decline"
        if d20 >= BRX["watch"] and bdd >= BRX["near_high"]:
            return "watch"
        return None

    out = {}
    for name, g, b in (("sp500", mem, "SPY"), ("tech", tech, "XLK"), ("ex_tech", rest, None)):
        d20, a50, n_ok = snap(g, i)
        d20p, a50p, _ = snap(g, i - 20)
        bdd = bench_dd(b, i)
        out[name] = {"members": len(g), "with_52w_high": n_ok,
                     "pct_down20": d20, "pct_down20_4w_ago": d20p,
                     "pct_above_50sma": a50, "pct_above_50sma_4w_ago": a50p,
                     "bench": b, "bench_pct_from_52w_high": bdd,
                     "label": label(d20, bdd) if b else None}
    t, r = out["tech"]["pct_down20"], out["ex_tech"]["pct_down20"]
    C = P.C
    q_rel = None
    if "QQQ" in C.columns and i >= 21:
        q_rel = round(100 * float((C["QQQ"].iloc[i] / C["QQQ"].iloc[i - 21]) / (C[BENCH].iloc[i] / C[BENCH].iloc[i - 21]) - 1), 2)
    rsp4 = None
    if "RSP" in C.columns and i >= 20:
        rsp4 = round(100 * float((C["RSP"].iloc[i] / C[BENCH].iloc[i]) / (C["RSP"].iloc[i - 20] / C[BENCH].iloc[i - 20]) - 1), 2)
    mega = None if q_rel is None or rsp4 is None else bool(q_rel <= BRX["megacap_qqq_rel_1m"] and rsp4 >= BRX["megacap_rsp_spy_4w"])
    return {"definition_version": "BX-1.0", "as_of_index": int(i), "groups": out,
            "tech_minus_ex_tech_down20_pt": round(t - r, 1) if t is not None and r is not None else None,
            "megacap_led": mega, "megacap_inputs": {"qqq_rel_spy_1m_pct": q_rel, "rsp_spy_4w_pct": rsp4}}


# ----------------------------------------------------------------------------
# セットアップ検出（E9: VCP / Power Play / 早期エントリー）
# ----------------------------------------------------------------------------
def swings(h, l, strict=2):
    """確認済みスイング高値・安値（前後strict本より厳密に高い/低い）。(index位置, 値) のリスト。"""
    hv, lv = h.values, l.values
    n = len(hv)
    sh, sl = [], []
    for j in range(strict, n - strict):
        w = slice(j - strict, j + strict + 1)
        if np.isnan(hv[j]) or np.isnan(lv[j]):
            continue
        if hv[j] > np.nanmax(np.delete(hv[w], strict)):
            sh.append((j, hv[j]))
        if lv[j] < np.nanmin(np.delete(lv[w], strict)):
            sl.append((j, lv[j]))
    return sh, sl


def detect_vcp(P, t, i, max_window=130, min_window=15):
    """銘柄t、日付位置iでのVCP候補（E9.1のP基準）。戻り値は dict または None。"""
    h, l, c = P.H[t].iloc[: i + 1], P.L[t].iloc[: i + 1], P.C[t].iloc[: i + 1]
    if len(c) < 260 or c.iloc[-max_window:].isna().any():
        return None
    sh, sl = swings(h.iloc[-max_window - 5:], l.iloc[-max_window - 5:])
    off = len(c) - (max_window + 5)
    sh = [(j + off, v) for j, v in sh]
    sl = [(j + off, v) for j, v in sl]
    if not sh:
        return None
    # ベースの起点＝窓内の最高スイング高値
    j0, H0 = max(sh, key=lambda x: x[1])
    if i - j0 < min_window:
        return None
    # 収縮列: H0 以降の (高値→次の安値) を順にたどる
    contractions, cur_hi_j, cur_hi = [], j0, H0
    lows_after = [x for x in sl if x[0] > j0]
    highs_after = [x for x in sh if x[0] > j0]
    while True:
        nl = [x for x in lows_after if x[0] > cur_hi_j]
        if not nl:
            break
        jl, L1 = nl[0]
        contractions.append({"from": cur_hi_j, "to": jl, "high": cur_hi, "low": L1, "depth": (cur_hi - L1) / cur_hi})
        nh = [x for x in highs_after if x[0] > jl]
        if not nh:
            break
        cur_hi_j, cur_hi = nh[0]
        if cur_hi > H0 * 1.0:
            # 新高値で基盤が更新されたら、その高値から数え直し
            H0, j0, contractions = cur_hi, cur_hi_j, []
    if len(contractions) < 2:
        return None
    depths = [x["depth"] for x in contractions]
    decreasing = all(depths[k + 1] < depths[k] for k in range(len(depths) - 1))
    last = contractions[-1]
    rng5 = (h.iloc[-5:].max() - l.iloc[-5:].min()) / h.iloc[-5:].max()
    dry5 = float(P.dry5[t].iloc[i]) if not math.isnan(P.dry5[t].iloc[i]) else None
    pivot = float(h.iloc[last["from"]: i + 1].max())
    stop = float(last["low"] * 0.995)
    e_plan = pivot * 1.001
    risk = (e_plan - stop) / e_plan
    # 閾値はP。2025-01〜2026-10の母集団73銘柄で校正（仕様E9.1の注記参照）。
    checks = {
        "contractions_2_6": 2 <= len(contractions) <= 6,
        "depth_decreasing": decreasing,
        "final_depth_ok": last["depth"] <= 0.10 and last["depth"] <= 0.6 * depths[0],
        "range5_ok": rng5 <= 0.06,
        "dry5_ok": dry5 is not None and dry5 <= 0.75,
        "risk_ok": risk <= 0.07,
        "not_extended": c.iloc[-1] <= pivot * 1.05,
        "near_pivot": c.iloc[-1] >= pivot * 0.92,
    }
    core = checks["contractions_2_6"] and checks["depth_decreasing"] and last["depth"] <= 0.12 and last["depth"] <= 0.6 * depths[0]
    if not core:
        return None
    if all(checks.values()):
        status = "ready"
    elif rng5 <= 0.08 and dry5 is not None and dry5 <= 1.0 and risk <= 0.08 and checks["not_extended"] and checks["near_pivot"]:
        status = "watch"      # 収縮は完成に近いが、出来高の枯渇か値幅の締まりが未達
    else:
        status = "forming"
    return {
        "pattern": "vcp",
        "status": status,
        "base_start": str(c.index[j0].date()),
        "base_days": int(i - j0),
        "contractions": [round(100 * d, 1) for d in depths],
        "footprint": f"{round((i - j0) / 5)}W {round(100 * depths[0])}/{round(100 * depths[-1])} {len(depths)}T",
        "pivot": round(pivot, 2),
        "stop": round(stop, 2),
        "risk_pct": round(100 * risk, 1),
        "dist_to_pivot_pct": round(100 * (c.iloc[-1] / pivot - 1), 1),
        "range5_pct": round(100 * rng5, 1),
        "dry5": round(dry5, 2) if dry5 is not None else None,
        "checks": checks,
    }


def detect_power_play(P, t, i):
    """E9.3: 40営業日以内に+100%の急騰後、10〜30日の浅い横ばい。"""
    c, h, l = P.C[t].iloc[: i + 1], P.H[t].iloc[: i + 1], P.L[t].iloc[: i + 1]
    if len(c) < 120 or c.iloc[-80:].isna().any():
        return None
    for cons in range(10, 31):
        run_end = i - cons
        seg = c.iloc[run_end - 40: run_end + 1]
        if seg.empty:
            continue
        lo = seg.min()
        if c.iloc[run_end] / lo - 1 >= 1.0 and c.iloc[run_end] == c.iloc[run_end - 40: i + 1].max():
            cons_hi, cons_lo = h.iloc[run_end: i + 1].max(), l.iloc[run_end + 1: i + 1].min()
            if (cons_hi - cons_lo) / cons_hi <= 0.25:
                dry5 = float(P.dry5[t].iloc[i]) if not math.isnan(P.dry5[t].iloc[i]) else None
                pivot, stop = float(cons_hi), float(cons_lo * 0.995)
                risk = (pivot * 1.001 - stop) / (pivot * 1.001)
                ok = dry5 is not None and dry5 <= 0.7 and risk <= 0.07 and c.iloc[-1] <= pivot * 1.05
                return {
                    "pattern": "power_play", "status": "ready" if ok else "forming",
                    "run_pct": round(100 * (c.iloc[run_end] / lo - 1)), "consolidation_days": cons,
                    "max_pullback_pct": round(100 * (cons_hi - cons_lo) / cons_hi, 1),
                    "pivot": round(pivot, 2), "stop": round(stop, 2), "risk_pct": round(100 * risk, 1),
                    "dist_to_pivot_pct": round(100 * (c.iloc[-1] / pivot - 1), 1), "dry5": round(dry5, 2) if dry5 else None,
                }
    return None


def detect_early_entry(P, t, i, vcp):
    """E9.4: ベース内の下側〜中央1/3にある3〜10日の小さな休止（チート／ロー・チート候補）。"""
    if vcp is None:
        return None
    c, h, l = P.C[t].iloc[: i + 1], P.H[t].iloc[: i + 1], P.L[t].iloc[: i + 1]
    j0 = c.index.get_loc(pd.Timestamp(vcp["base_start"]))
    base_hi, base_lo = h.iloc[j0: i + 1].max(), l.iloc[j0: i + 1].min()
    for n in range(3, 11):
        seg_h, seg_l = h.iloc[i - n + 1: i + 1], l.iloc[i - n + 1: i + 1]
        rng = (seg_h.max() - seg_l.min()) / seg_h.max()
        if rng > 0.06:
            continue
        pos = (seg_h.max() - base_lo) / (base_hi - base_lo) if base_hi > base_lo else 1
        if pos >= 2 / 3:
            continue
        dry5 = float(P.dry5[t].iloc[i]) if not math.isnan(P.dry5[t].iloc[i]) else None
        if dry5 is None or dry5 > 0.8:
            continue
        pivot, stop = float(seg_h.max()), float(seg_l.min() * 0.995)
        risk = (pivot * 1.001 - stop) / (pivot * 1.001)
        return {"pattern": "low_cheat" if pos < 1 / 3 else "cheat", "status": "ready" if risk <= 0.07 else "forming",
                "pause_days": n, "pause_range_pct": round(100 * rng, 1), "position_in_base": round(pos, 2),
                "pivot": round(pivot, 2), "stop": round(stop, 2), "risk_pct": round(100 * risk, 1),
                "dist_to_pivot_pct": round(100 * (c.iloc[-1] / pivot - 1), 1), "dry5": round(dry5, 2)}
    return None


# ----------------------------------------------------------------------------
# v4.6b（07追補）: ステージ分析（E6.2）、業種別のステージ分布（E6.3）、ベースカウント（E9.5a）
# ----------------------------------------------------------------------------
def _d(P, j):
    return str(P.dates[j].date())


STAGE_JA = {"1A": "底固めの始まり", "1": "底固め", "1B": "底固めの終盤（上抜け間近）",
            "2A": "上昇の初期", "2": "上昇の途中", "2B": "上昇の終盤",
            "3A": "天井の兆し", "3": "天井圏", "3B": "天井圏の終盤（崩れ間近）",
            "4A": "下落入り", "4": "下落の途中", "4B": "下落の終盤", "4B-": "安値を付けた可能性"}


def weekly_bars(P, t, i):
    """完了週と進行中の週の週足（W-FRI）。戻り値 (DataFrame, 最後の週が未完了か)。"""
    df = pd.DataFrame({"c": P.C[t].iloc[: i + 1], "h": P.H[t].iloc[: i + 1], "l": P.L[t].iloc[: i + 1],
                       "v": P.V[t].iloc[: i + 1]}).dropna(subset=["c", "h", "l"])
    if df.empty:
        return df, False
    key = df.index.to_period("W-FRI")
    g = df.groupby(key)
    W = pd.DataFrame({"c": g["c"].last(), "h": g["h"].max(), "l": g["l"].min(), "v": g["v"].sum(min_count=1)})
    dates = pd.Series(df.index, index=df.index).groupby(key)
    W["first"], W["last"] = dates.first().values, dates.last().values
    partial = pd.Timestamp(W["last"].iloc[-1]).weekday() != 4   # 金曜で終わっていない最後の週は未完了
    return W, partial


def stage_machine(W, spy_c):
    """E6.2（07追補）：週足の状態遷移でステージ（1〜4）と細区分（1A〜4B-）を決める。各週の結果のリストを返す。"""
    n = len(W)
    c, h, l, v = W["c"].values, W["h"].values, W["l"].values, W["v"].values
    ma = W["c"].rolling(30).mean().values
    s4 = (pd.Series(ma) / pd.Series(ma).shift(4) - 1).values * 100
    d = (c / ma - 1) * 100
    ratio = pd.Series(c, index=W.index) / spy_c.reindex(W.index)
    mrs = ((ratio / ratio.rolling(STG["mrs_weeks"], min_periods=STG["mrs_weeks"]).mean() - 1) * 100).values
    sw_l = [k for k in range(1, n - 1) if l[k] < l[k - 1] and l[k] < l[k + 1]]
    flat = STG["flat"]
    out = [None] * n
    stage, st_start, trunc = None, None, False
    adv_start, H2, s4max, sup2 = None, None, None, None      # ステージ2（上昇）の起点・高値・線の傾きの最大・支え
    sup3, R3 = None, None                                      # ステージ3（天井圏）の支え・上限
    low4, low4_w, s4min = None, None, None                     # ステージ4（下落）の安値・その週・線の傾きの最小
    base_from, breakout, resume = None, None, False            # ステージ1（底固め）の起点、直近のブレイク、天井圏からの上昇再開
    adv_trunc, last_change = False, None                       # 上昇の起点がデータより前か、直近のステージ変更
    above, below, flatn = 0, 0, 0
    for w in range(n):
        if np.isnan(s4[w]) or np.isnan(d[w]):
            continue
        above = above + 1 if c[w] > ma[w] else 0
        below = below + 1 if c[w] < ma[w] else 0
        flatn = flatn + 1 if abs(s4[w]) <= flat else 0
        lows_ok = [k for k in sw_l if k + 1 <= w]
        new, why = None, ""
        if stage is None:                       # 最初の分類（データの始まり。起点は不明＝truncated）
            org = ma[w] / ma[w - 26] if w >= 26 and not np.isnan(ma[w - 26]) else np.nan
            if above >= 2 and s4[w] > flat:
                new, why = 2, "初回：上向きの30週線の上"
            elif below >= 2 and s4[w] < -flat:
                new, why = 4, "初回：下向きの30週線の下"
            elif flatn >= 2 and abs(d[w]) <= STG["near_ma"] and not np.isnan(org):
                new, why = (3, "初回：上昇のあと線が横ばい") if org > 1.10 else (1, "初回：下落のあと線が横ばい") if org < 0.90 else (None, "")
            if new is None:
                continue
            trunc = True
        elif stage == 1:
            res = np.nanmax(h[base_from: w]) if w > base_from else np.nan
            base_low = np.nanmin(l[base_from: w]) if w > base_from else np.nan
            if not np.isnan(res) and c[w] > res and c[w] > ma[w] and s4[w] >= -STG["bo_min_slope"]:
                new, why = 2, "底固めの上限を終値で上抜け"
                vb = np.nanmean(v[max(0, w - 4): w])
                breakout = {"week": w, "level": float(res), "vol_ratio": float(v[w] / vb) if vb and vb > 0 else None}
            elif not np.isnan(base_low) and (s4[w] < 0 or d[w] <= -STG["break_below_ma"]) and (
                    c[w] < base_low * (1 - STG["base_fail_pct"] / 100) or (c[w] < base_low and w >= 1 and c[w - 1] < base_low)):
                new, why = 4, "底固めの安値を終値で割った（3%超、または2週続けて）"
        elif stage == 2:
            low13 = np.nanmin(l[max(0, w - STG["support_weeks"]): w]) if w > 0 else np.nan
            if c[w] < ma[w] and c[w] < low13 and (d[w] <= -STG["break_below_ma"] or s4[w] < 0):
                new, why = 4, f"30週線を{-d[w]:.0f}%下回り、直近13週の安値も終値で割った（天井圏を経ない崩れ）"
            elif (adv_start is not None and w - adv_start <= STG["a_weeks"] and breakout and not adv_trunc
                  and c[w] < breakout["level"] and c[w] < ma[w]):
                new, why = 1, "上抜けから8週以内に底固めの上限と30週線の下へ戻った（上抜け失敗）"
            elif below >= 2:
                new, why = 3, "30週線の下で2週続けて引けた"
            elif flatn >= 2 and abs(d[w]) <= STG["near_ma"] and (adv_start is None or w - adv_start > STG["a_weeks"]):
                new, why = 3, "30週線が横ばいになり、株価が線の近く"
        elif stage == 3:
            if c[w] < ma[w] and c[w] < sup3:
                new, why = 4, "天井圏の安値（支え）を終値で割った"
            elif below >= 2 and s4[w] < -flat:
                new, why = 4, "30週線が下向きに変わり、2週続けて線の下"
            elif c[w] > R3 and c[w] > ma[w] and s4[w] > 0:
                new, why, resume = 2, "天井圏の上限を上抜けて上昇再開", True
        elif stage == 4:
            if above >= 2 and s4[w] > flat:
                new, why = 3, "下落入りの後、まだ上向きの30週線の上に戻った（天井圏の荒い値動き）"
            elif above >= 2 and s4[w] >= -STG["s4_to1_slope"]:
                new, why = 1, "30週線の上で2週続けて引けた"
            elif flatn >= 2 and abs(d[w]) <= STG["near_ma"] and w - st_start >= STG["s4_min_weeks"]:
                new, why = 1, "30週線が横ばいになり、株価が線の近く"
        if new is not None:
            prev = stage
            stage, st_start = new, w
            if prev is not None:
                trunc = False                                  # 実際の遷移で始まったステージ（起点が分かる）
            if new == 2:
                if prev == 3 and resume and adv_start is not None:   # 天井圏からの上昇再開：同じ上昇の続き（起点・ベースの番号を引き継ぐ）
                    H2, s4max = max(H2 or h[w], h[w]), (s4max if s4max is not None else s4[w])
                else:
                    adv_start, H2, s4max = w, h[w], s4[w]
                    adv_trunc = prev is None
                    if prev != 1:
                        breakout = None
                resume = False
            elif new == 3:
                sup3 = float(np.nanmin(l[max(0, w - STG["support_weeks"]): w + 1]))
                if prev == 4:                                   # 崩れの後の戻り：支え＝下落の安値、上限＝直近26週の高値
                    sup3 = float(min(sup3, low4))
                    H2 = None
                R3 = max(H2 if H2 is not None else np.nanmax(h[max(0, w - 26): w + 1]), h[w])
            elif new == 4:
                low4, low4_w, s4min = l[w], w, s4[w]
                adv_start, H2, s4max, sup2, breakout, adv_trunc = None, None, None, None, None, False
            elif new == 1:
                if prev == 2:
                    pass                                        # 上抜け失敗：同じ底固めの続き（base_from を引き継ぐ）
                elif prev == 4 and low4_w is not None:
                    base_from = low4_w
                else:
                    base_from = max(0, w - 26) + int(np.nanargmin(l[max(0, w - 26): w + 1]))
            last_change = {"week": w, "from": prev, "to": new, "why": why}
        if stage is None:
            continue
        # 状態の更新
        if stage == 2:
            H2, s4max = max(H2, h[w]), max(s4max, s4[w])
            cand = [l[k] for k in lows_ok if adv_start is not None and k >= adv_start]
            sup2 = cand[-1] if cand else (np.nanmin(l[adv_start: w + 1]) if adv_start is not None else l[w])
        elif stage == 3:
            R3 = max(R3, h[w])
            if w > st_start:
                sup3 = min(sup3, np.nanmin(l[st_start: w]))
        elif stage == 4:
            if l[w] < low4:
                low4, low4_w = l[w], w
            s4min = min(s4min, s4[w])
        # 細区分
        wk = w - st_start
        wk1 = wk + 1                                          # 何週目か（入った週＝1週目）
        reasons, sub = [], str(stage)
        mrs_w, mrs_4 = mrs[w], (mrs[w - 4] if w >= 4 else np.nan)
        rs_up = bool(not np.isnan(mrs_w) and not np.isnan(mrs_4) and mrs_w > mrs_4)
        if stage == 1:
            res = np.nanmax(h[base_from: w]) if w > base_from else h[w]
            bw = w - base_from
            if bw >= STG["s1_late_weeks"] and s4[w] >= -STG["bo_min_slope"] and c[w] >= res * (1 - STG["s1b_near_res"] / 100) and rs_up:
                sub = "1B"; reasons.append(f"底固め{bw}週、上限まで{100 * (res / c[w] - 1):.1f}%、市場比の強さが改善")
            elif wk < STG["s1_early_weeks"] or s4[w] < -STG["bo_min_slope"]:
                sub = "1A"; reasons.append("下落が止まったばかり" if wk < STG["s1_early_weeks"] else "30週線はまだ下向き")
            if d[w] >= 15:
                reasons.append(f"30週線から+{d[w]:.0f}%の急な戻り（V字。底固めの形はまだない）")
        elif stage == 2:
            age = w - adv_start if adv_start is not None else None
            flags = []
            if adv_trunc or (age is not None and age >= STG["b_age"]):
                flags.append("上昇がデータの始まりより前から続いている" if adv_trunc else f"上昇{age}週")
            if s4max >= 3 and s4[w] < 0.5 * s4max:
                flags.append(f"30週線の傾きが鈍化（{s4[w]:.1f}%、最大{s4max:.1f}%）")
            if w >= STG["b_div_weeks"] and c[w] >= 0.9 * H2 and not np.isnan(mrs_w) and not np.isnan(mrs[w - STG["b_div_weeks"]]) and mrs_w < mrs[w - STG["b_div_weeks"]]:
                flags.append("高値圏なのに市場比の強さが低下")
            if sup2 is not None and c[w] < sup2:
                flags.append("直近の押し安値を割った")
            if any(c[k] < ma[k] for k in range(max(st_start, w - 7), w)):
                flags.append("直近8週に30週線を割った週あり")
            if len(flags) >= 2 or "直近の押し安値を割った" in flags:
                sub = "2B"; reasons += flags
            elif not adv_trunc and age is not None and age <= STG["a_weeks"] and d[w] <= STG["a_max_ext"]:
                sub = "2A"; reasons.append(f"上抜けから{age + 1}週目" + ("（上抜けの週の出来高は直前4週平均の{:.1f}倍）".format(breakout["vol_ratio"]) if breakout and breakout.get("vol_ratio") else ""))
            else:
                reasons += flags
            if d[w] >= STG["b_ext"]:
                reasons.append(f"30週線から+{d[w]:.0f}%（離れすぎ。新規は押し目待ち）")
        elif stage == 3:
            near_sup = c[w] <= sup3 * (1 + STG["s3_near_support"] / 100)
            late = wk >= STG["s3_late_weeks"] or (s4[w] < 0 and c[w] < ma[w]) or near_sup
            if late:
                sub = "3B"
                reasons.append("支えの近く" if near_sup else "30週線が下向きに変わり、株価も線の下" if (s4[w] < 0 and c[w] < ma[w]) else f"天井圏{wk1}週目")
            elif wk < STG["s3_early_weeks"]:
                sub = "3A"
        elif stage == 4:
            nolow = w - low4_w
            if nolow >= STG["s4_nolow_bminus"] and d[w] >= -STG["s4_bminus_near_ma"]:
                sub = "4B-"; reasons.append(f"{nolow}週安値を更新せず、30週線の近く")
            elif (wk >= STG["s4_late_weeks"] and nolow >= STG["s4_nolow_late"]) or (wk >= 8 and s4min < 0 and s4[w] > 0.5 * s4min):
                sub = "4B"; reasons.append(f"{nolow}週安値を更新せず" if nolow >= STG["s4_nolow_late"] else "下げの勢いが鈍化")
            elif wk < STG["s4_early_weeks"]:
                sub = "4A"; reasons.append(f"下落入りから{wk1}週目")
        qual = "+" if (not np.isnan(mrs_w) and mrs_w > 0 and rs_up) else "-" if (not np.isnan(mrs_w) and mrs_w < 0 and not rs_up) else ""
        out[w] = {"stage": stage, "sub": sub, "qual": qual, "weeks": int(wk1), "start": int(st_start), "trunc": bool(trunc),
                  "adv_start": adv_start, "adv_trunc": bool(adv_trunc), "reasons": reasons, "ma": float(ma[w]), "d": float(d[w]), "s4": float(s4[w]),
                  "mrs": None if np.isnan(mrs_w) else float(mrs_w), "mrs_4w": None if np.isnan(mrs_4) else float(mrs_4),
                  "support": float(sup2 if stage == 2 else sup3 if stage == 3 else np.nanmin(l[base_from: w + 1]) if stage == 1 else low4) if stage else None,
                  "resistance": float(R3 if stage == 3 else np.nanmax(h[base_from: w]) if stage == 1 and w > base_from else H2 if stage == 2
                                      else ma[w] if stage == 4 else np.nan),
                  "low13": float(np.nanmin(l[max(0, w - STG["support_weeks"]): w])) if w > 0 else None,
                  "breakout": breakout if stage == 2 else None, "change": last_change}
    return out


def weekly_stage(P, t, i, spy_w=None):
    """E6.2（07追補）：週足のステージと細区分。公式の判定は完了した週だけで行い、進行中の週は暫定として別に出す。"""
    out = {"stage": None, "sub": None, "label": None, "label_ja": None, "qual": None, "since": None, "weeks_in_stage": None, "low_13w": None,
           "stage2_start": None, "truncated": None, "pct_vs_ma30": None, "ma30": None, "ma30_slope_4w_pct": None,
           "mansfield_rs": None, "mansfield_rs_4w_ago": None, "support": None, "resistance": None, "breakout": None,
           "reasons": [], "last_change": None, "week_end": None, "provisional_week": None, "provisional": None,
           "stage_1w_ago": None, "stage_4w_ago": None, "sub_4w_ago": None, "history": None, "note": None}
    W, partial = weekly_bars(P, t, i)
    if len(W) < 36:
        out["note"] = "履歴不足"
        return out
    if spy_w is None:
        spy_w, _ = weekly_bars(P, BENCH, i)
    res = stage_machine(W, spy_w["c"])
    k = len(W) - 2 if partial else len(W) - 1          # 公式＝最後の完了週
    r = res[k] if k >= 0 else None
    if r is None:
        out["note"] = "履歴不足（ステージを判定できる週がまだない）"
        return out
    wd = lambda j: str(pd.Timestamp(W["first"].iloc[j]).date())
    lab = r["sub"] + (f"({r['qual']})" if r["qual"] else "")
    out.update({"stage": r["stage"], "sub": r["sub"], "label": lab, "label_ja": STAGE_JA.get(r["sub"], ""), "qual": r["qual"] or None,
                "since": wd(r["start"]), "weeks_in_stage": r["weeks"], "truncated": r["trunc"],
                "pct_vs_ma30": round(r["d"], 1), "ma30": round(r["ma"], 2), "ma30_slope_4w_pct": round(r["s4"], 2),
                "mansfield_rs": None if r["mrs"] is None else round(r["mrs"], 1), "mansfield_rs_4w_ago": None if r["mrs_4w"] is None else round(r["mrs_4w"], 1),
                "support": None if r["support"] is None or np.isnan(r["support"]) else round(r["support"], 2),
                "resistance": None if r["resistance"] is None or np.isnan(r["resistance"]) else round(r["resistance"], 2),
                "reasons": r["reasons"], "week_end": str(pd.Timestamp(W["last"].iloc[k]).date()),
                "low_13w": None if r["low13"] is None or np.isnan(r["low13"]) else round(r["low13"], 2)})
    if r["stage"] == 2 and r["adv_start"] is not None:
        out["stage2_start"] = wd(r["adv_start"])
        out["truncated"] = r["adv_trunc"]
    if r["breakout"]:
        b = r["breakout"]
        out["breakout"] = {"week_start": wd(b["week"]), "level": round(b["level"], 2),
                           "vol_ratio_vs_4w": round(b["vol_ratio"], 2) if b.get("vol_ratio") else None}
    ch = r["change"]
    if ch:
        out["last_change"] = {"week_start": wd(ch["week"]), "from": ch["from"], "to": ch["to"], "why": ch["why"]}
    if k >= 1 and res[k - 1]:
        out["stage_1w_ago"] = res[k - 1]["sub"]
    if k >= 4 and res[k - 4]:
        out["stage_4w_ago"], out["sub_4w_ago"] = res[k - 4]["stage"], res[k - 4]["sub"]
    out["history"] = "/".join((x["sub"] if x else "-") for x in res[max(0, k - 7): k + 1])   # 直近8週（古い順）
    if partial:
        p = res[-1]
        out["provisional_week"] = str(pd.Timestamp(W["last"].iloc[-1]).date())
        if p and (p["sub"] != r["sub"] or p["stage"] != r["stage"]):
            out["provisional"] = {"label": p["sub"] + (f"({p['qual']})" if p["qual"] else ""), "label_ja": STAGE_JA.get(p["sub"], ""), "reasons": p["reasons"],
                                  "why": p["change"]["why"] if p["change"] and p["change"]["week"] == len(W) - 1 else None}
    return out


SB_LABEL_JA = {"weak": "弱い（ステージ4が4割以上）", "deteriorating": "悪化中（4週でステージ4、またはステージ3＋4が10pt以上増、またはステージ3が4割以上）",
               "healthy": "健全（ステージ2が5割以上、ステージ4が2割未満）",
               "improving": "改善中（4週でステージ2が10pt以上増、または上抜け間近と上昇初期が25%以上。天井圏・下落中が増えていないこと）", "neutral": "どちらとも言えない"}
SUBS = ("1A", "1", "1B", "2A", "2", "2B", "3A", "3", "3B", "4A", "4", "4B", "4B-")


def _sub_at(st, k):
    """k週前の細区分（history は直近8週、古い順）。"""
    h = (st.get("history") or "").split("/")
    return h[-1 - k] if len(h) > k and h[-1 - k] not in ("", "-") else None


def stage_breadth(stages, groups_map):
    """E6.3（07追補）：業種ごとの個別株ステージの分布と調子のラベル。今・1週前・4週前・5週前。"""
    def agg(members, k):
        subs = [_sub_at(stages[t], k) for t in members if t in stages]
        subs = [x for x in subs if x]
        n = len(subs)
        if not n:
            return None
        main = {m: sum(1 for x in subs if x[0] == str(m)) for m in (1, 2, 3, 4)}
        pct = {m: round(100 * main[m] / n, 1) for m in main}
        fresh = round(100 * sum(1 for x in subs if x in ("1B", "2A")) / n, 1)
        return {"n": n, "count": main, "pct": pct, "pct_1B_2A": fresh,
                "by_sub": {x: sum(1 for y in subs if y == x) for x in SUBS if any(y == x for y in subs)}}

    def label(now, w4):
        if now is None:
            return None
        p, q = now["pct"], (w4 or {}).get("pct") or {}
        d4 = p[4] - q[4] if q else 0
        d2 = p[2] - q[2] if q else 0
        d34 = (p[3] + p[4]) - (q[3] + q[4]) if q else 0      # 天井圏＋下落中の増減（v4.6c：上昇中が増えても天井圏・下落中が同時に増えていれば改善とは言わない）
        if p[4] >= 40:
            return "weak"
        if d4 >= 10 or p[3] >= 40 or d34 >= 10:
            return "deteriorating"
        if p[2] >= 50 and p[4] < 20:
            return "healthy"
        if (d2 >= 10 or now["pct_1B_2A"] >= 25) and d34 <= 0:
            return "improving"
        return "neutral"

    out = {}
    for g, members in groups_map.items():
        now, w1, w4, w5 = (agg(members, k) for k in (0, 1, 4, 5))
        if now is None:
            continue
        lab, lab_1w = label(now, w4), label(w1, w5)
        above = [stages[t]["pct_vs_ma30"] for t in members if t in stages and stages[t].get("pct_vs_ma30") is not None]
        tick = {}
        for t in members:
            st = stages.get(t) or {}
            if st.get("sub"):
                tick.setdefault(st["sub"], []).append(t)
        out[g] = {"n": now["n"], "pct_stage": now["pct"], "count_stage": now["count"], "by_sub": now["by_sub"],
                  "pct_1B_2A": now["pct_1B_2A"], "pct_above_ma30": round(100 * sum(1 for x in above if x > 0) / len(above), 1) if above else None,
                  "pct_stage_4w_ago": (w4 or {}).get("pct"), "pct_stage_1w_ago": (w1 or {}).get("pct"),
                  "net_2_minus_4": round(now["pct"][2] - now["pct"][4], 1),
                  "net_2_minus_4_4w_ago": round(w4["pct"][2] - w4["pct"][4], 1) if w4 else None,
                  "label": lab, "label_ja": SB_LABEL_JA.get(lab), "label_1w_ago": lab_1w,
                  "tickers_by_sub": {k: sorted(v) for k, v in tick.items()} if len(members) <= 80 else
                                    {k: sorted(v)[:15] for k, v in tick.items() if k in ("1B", "2A")}}
    return out


def base_walk(P, t, i, start):
    """E9.5a: Stage2の起点から日足でベースを数える。ベース＝高値から8%以上の押しで、5週以上かけて高値を終値で上抜けたもの。"""
    if start is None:
        return None
    c, h, l = P.C[t].values, P.H[t].values, P.L[t].values
    j_s = P.dates.get_loc(pd.Timestamp(start))
    peak, jpeak, in_corr, low = h[j_s], j_s, False, None
    bases, prev_low, count, resets = [], None, 0, []
    for j in range(j_s + 1, i + 1):
        if math.isnan(c[j]):
            continue
        if not in_corr:
            if h[j] > peak:
                peak, jpeak = h[j], j
            elif l[j] <= peak * (1 - BC["min_depth"]):
                in_corr, low = True, l[j]
            continue
        low = min(low, l[j])
        if (peak - low) / peak > BC["max_depth"]:      # 50%超はベースではない。数え直す
            resets.append({"date": _d(P, j), "reason": "深さ50%超"})
            count, prev_low, peak, jpeak, in_corr = 0, None, h[j], j, False
            continue
        if c[j] > peak:
            if j - jpeak >= BC["min_days"]:
                if prev_low is not None and low < prev_low:
                    count = 1
                    resets.append({"date": _d(P, j), "reason": "前のベースの安値を下回った"})
                else:
                    count += 1
                prev_low = low
                bases.append({"n": count, "start": _d(P, jpeak), "breakout": _d(P, j), "days": j - jpeak,
                              "depth_pct": round(100 * (peak - low) / peak, 1), "high": round(float(peak), 2), "low": round(float(low), 2)})
            peak, jpeak, in_corr = h[j], j, False
    cur = None
    if in_corr:
        cur = {"n": 1 if (prev_low is not None and low < prev_low) else count + 1, "start": _d(P, jpeak), "days": i - jpeak,
               "depth_pct": round(100 * (peak - low) / peak, 1), "high": round(float(peak), 2),
               "provisional": i - jpeak < BC["min_days"]}
    return {"stage2_start": start, "completed": bases, "count_completed": count, "current": cur, "resets": resets[-3:]}


def base_number(stage, walk, base_start, base_low):
    """セットアップのベースが何番目か（E9.5）。戻り値は (番号 or None, 状態)。"""
    if stage is None or stage.get("stage") != 2:
        return None, "対象外（ステージ2でない。次のステージ2で1から数え直す）"
    if stage.get("truncated") or walk is None:
        return None, "未確認（上昇の起点がデータより前）"
    done = [b for b in walk["completed"] if b["breakout"] <= base_start]
    if not done:
        return 1, "ok"
    last = done[-1]
    return (1 if base_low < last["low"] else last["n"] + 1), "ok"


# ----------------------------------------------------------------------------
# v4.6（07追補）: カップウィズハンドル（E9.2a）、フラットベース（E9.6）、ダブルボトム（E9.7）
# ----------------------------------------------------------------------------
def _ready_status(c_last, pivot, stop, extra_ok, watch_ok=True):
    e_plan = pivot * 1.001
    risk = (e_plan - stop) / e_plan
    checks = {"risk_ok": risk <= READY["max_risk"], "not_extended": c_last <= pivot * READY["extended"],
              "near_pivot": c_last >= pivot * READY["near_pivot"]}
    checks.update(extra_ok)
    if all(checks.values()):
        status = "ready"
    elif watch_ok and risk <= READY["watch_risk"] and checks["not_extended"] and checks["near_pivot"]:
        status = "watch"
    else:
        status = "forming"
    return status, risk, checks


def _prior_up(l, j0, high):
    lo = np.nanmin(l[max(0, j0 - 130): j0 + 1])
    return high / lo - 1 if lo and lo > 0 else np.nan


def detect_cup(P, t, i):
    """E9.2a: カップウィズハンドル（IBD教材版：深さ15〜30%、7週以上、先行上昇30%以上、ハンドルは上半分）。"""
    c, h, l, v = P.C[t].values[: i + 1], P.H[t].values[: i + 1], P.L[t].values[: i + 1], P.V[t].values[: i + 1]
    if len(c) < 260 or np.isnan(c[-60:]).any():
        return None
    seg = h[i - CUP["handle_max"] + 1: i + 1]
    jR = i - CUP["handle_max"] + 1 + (len(seg) - 1 - int(np.argmax(seg[::-1])))   # ハンドル開始＝直近20日の最高値（同値なら新しい方）
    handle_len = i - jR + 1
    if handle_len < CUP["handle_min"]:
        return None
    R = h[jR]
    # 左側高値：ハンドル開始より20日以上前の確認済みスイング高値で、そこからハンドル開始までの高値がそれを2%超は上回らない、最も近いもの
    # （右側がわずかに左側を上回ってからハンドルを作る形も認める。ハンドル高値と左側高値の下限の関係は handle_high_85 で別に見る）
    j0, run_max = None, R
    for j in range(jR - 2 * CUP["side_min_days"], max(2, jR - CUP["max_total_days"]) - 1, -1):
        run_max = max(run_max, h[j + 1])
        if h[j] * CUP["right_max_vs_left"] >= run_max and h[j] > h[j - 1] and h[j] > h[j - 2] and h[j] > h[j + 1] and h[j] > h[j + 2]:
            j0 = j
            break
    if j0 is None:
        return None
    H0 = h[j0]
    jB = j0 + int(np.nanargmin(l[j0: jR + 1]))
    B = l[jB]
    depth = (H0 - B) / H0
    HL = np.nanmin(l[jR: i + 1])
    mid = B + (H0 - B) / 2
    prior = _prior_up(l, j0, H0)
    core = {
        "depth_15_30": CUP["min_depth"] <= depth <= CUP["max_depth"],
        "left_side_10d": jB - j0 >= CUP["side_min_days"],
        "right_side_10d": jR - jB >= CUP["side_min_days"],
        "total_7w": CUP["min_total_days"] <= i - j0 <= CUP["max_total_days"],
        "prior_up_30": bool(prior >= CUP["prior_up"]),
        "handle_5_20d": CUP["handle_min"] <= handle_len <= CUP["handle_max"],
        "handle_depth_12": (R - HL) / R <= CUP["handle_max_depth"],
        "handle_upper_half": HL > mid,
        "handle_high_85": R >= CUP["handle_hi_vs_left"] * H0,
    }
    if not all(core.values()):
        return None
    vol_handle = np.nanmean(v[jR: i + 1])
    vol_base = np.nanmean(v[max(0, jR - 50): jR])
    hv_ratio = vol_handle / vol_base if vol_base > 0 else np.nan
    pivot, stop = float(R), float(HL * 0.995)
    status, risk, checks = _ready_status(c[-1], pivot, stop, {"handle_vol_dry": bool(hv_ratio <= CUP["handle_vol_max"])})
    return {
        "pattern": "cup_handle", "status": status, "base_start": _d(P, j0), "base_low": round(float(B), 2), "base_days": int(i - j0),
        "depth_pct": round(100 * depth, 1), "handle_days": int(handle_len), "handle_depth_pct": round(100 * (R - HL) / R, 1),
        "handle_vol_ratio": round(float(hv_ratio), 2), "prior_up_pct": round(100 * prior), "left_high": round(float(H0), 2),
        "footprint": f"{round((i - j0) / 5)}W {round(100 * depth)}% H{handle_len}d {round(100 * (R - HL) / R)}%",
        "pivot": round(pivot, 2), "trigger_ibd": round(pivot + 0.10, 2), "stop": round(stop, 2), "risk_pct": round(100 * risk, 1),
        "dist_to_pivot_pct": round(100 * (c[-1] / pivot - 1), 1),
        "dry5": round(float(P.dry5[t].iloc[i]), 2) if not math.isnan(P.dry5[t].iloc[i]) else None, "checks": {**core, **checks},
    }


def detect_flat_base(P, t, i):
    """E9.6: フラットベース（5週以上、高値からの押し15%以内、先行上昇20%以上）。"""
    c, h, l = P.C[t].values[: i + 1], P.H[t].values[: i + 1], P.L[t].values[: i + 1]
    if len(c) < 260 or np.isnan(c[-FLAT["max_days"] - 5:]).any():
        return None
    w0 = i - FLAT["max_days"] + 1
    seg = h[w0: i + 1]
    H0 = float(np.nanmax(seg))
    if h[i] >= H0 and c[i] > np.nanmax(h[w0: i]):
        return None                                   # 今日が上抜けの日（台帳のブレイク判定に任せる）
    floor = H0 * (1 - FLAT["max_depth"])
    # 起点：高値の95%以上に届いた最も古い日で、そこから今日まで安値が高値の85%を下回らず、
    # かつ起点の週の高値からその後の最高値までが3%以内（上昇の途中から数えない＝横ばいの上限が平ら）
    js = None
    for j in range(w0, i + 1):
        if h[j] >= FLAT["top_band"] * H0 and np.nanmin(l[j: i + 1]) >= floor and H0 <= np.nanmax(h[j: j + 6]) * FLAT["flat_top"]:
            js = j
            break
    if js is None or i - js < FLAT["min_days"]:
        return None
    lo = float(np.nanmin(l[js: i + 1]))
    depth = (H0 - lo) / H0
    prior = _prior_up(l, js, h[js])
    if not prior >= FLAT["prior_up"]:
        return None
    dry5 = float(P.dry5[t].iloc[i]) if not math.isnan(P.dry5[t].iloc[i]) else None
    pivot = H0
    stop = float(np.nanmin(l[i - FLAT["stop_lookback"] + 1: i + 1]) * 0.995)
    status, risk, checks = _ready_status(c[-1], pivot, stop, {"dry5_ok": dry5 is not None and dry5 <= FLAT["dry5_max"]})
    return {
        "pattern": "flat_base", "status": status, "base_start": _d(P, js), "base_low": round(lo, 2), "base_days": int(i - js),
        "depth_pct": round(100 * depth, 1), "ideal_depth": depth <= FLAT["ideal_depth"], "prior_up_pct": round(100 * prior),
        "footprint": f"{round((i - js) / 5)}W {round(100 * depth)}%",
        "pivot": round(pivot, 2), "stop": round(stop, 2), "invalidation": round(lo * 0.995, 2), "risk_pct": round(100 * risk, 1),
        "dist_to_pivot_pct": round(100 * (c[-1] / pivot - 1), 1), "dry5": round(dry5, 2) if dry5 is not None else None, "checks": checks,
    }


def detect_double_bottom(P, t, i):
    """E9.7: ダブルボトム（7週以上、深さ15〜35%、二番底は一番底をわずかに割る（最大5%）のが標準、ピボット＝W字の真ん中の高値）。"""
    c, h, l, v = P.C[t].values[: i + 1], P.H[t].values[: i + 1], P.L[t].values[: i + 1], P.V[t].values[: i + 1]
    if len(c) < 260 or np.isnan(c[-60:]).any():
        return None
    lo_w = max(2, i - DB["max_days"])
    j0 = lo_w + int(np.nanargmax(h[lo_w: i + 1]))     # 左側高値＝期間内の最高値
    if i - j0 < DB["min_days"]:
        return None
    H0 = h[j0]
    sh = [j for j in range(j0 + DB["leg_min_days"], i - 1) if h[j] > h[j - 1] and h[j] > h[j - 2] and h[j] > h[j + 1] and h[j] > h[j + 2]]
    for jM in sorted(sh, key=lambda j: -h[j]):       # 真ん中の高値の候補を高い順に
        if jM - j0 < DB["leg_min_days"] or i - jM < DB["leg_min_days"]:
            continue
        ja = j0 + int(np.nanargmin(l[j0: jM]))
        jb = jM + 1 + int(np.nanargmin(l[jM + 1: i + 1]))
        La, Lb, M = l[ja], l[jb], h[jM]
        if jM - ja < DB["leg_min_days"] or jb - jM < DB["leg_min_days"]:
            continue
        lowest = min(La, Lb)
        depth = (H0 - lowest) / H0
        if not (DB["min_depth"] <= depth <= DB["max_depth"]):
            continue
        if not (La * (1 - DB["undercut_max"]) <= Lb <= La * (1 + DB["no_undercut_tol"])):
            continue
        if (M - La) / (H0 - La) < DB["mid_min_frac"]:
            continue
        after = c[jb + 1: i + 1]
        if len(after) and np.nanmax(after) > M:
            continue                                  # 二番底の後（今日を含む）、すでに真ん中の高値を終値で上抜けた
        prior = _prior_up(l, j0, H0)
        if not prior >= DB["prior_up"]:
            return None
        vavg = np.nanmean(v[max(0, ja - 50): ja])
        v_low2 = np.nanmean(v[max(jb - 2, 0): min(jb + 3, i + 1)])
        low_vol = v_low2 / vavg if vavg > 0 else np.nan
        pivot = float(M)
        stop = float(max(Lb, np.nanmin(l[i - DB["stop_lookback"] + 1: i + 1])) * 0.995)
        status, risk, checks = _ready_status(c[-1], pivot, stop, {"low2_vol_not_up": bool(low_vol <= DB["low_vol_max"])})
        return {
            "pattern": "double_bottom", "status": status, "base_start": _d(P, j0), "base_low": round(float(lowest), 2), "base_days": int(i - j0),
            "depth_pct": round(100 * depth, 1), "first_low": round(float(La), 2), "first_low_date": _d(P, ja),
            "second_low": round(float(Lb), 2), "second_low_date": _d(P, jb), "undercut": bool(Lb < La),
            "undercut_pct": round(100 * (1 - Lb / La), 1), "middle_high_date": _d(P, jM), "low2_vol_ratio": round(float(low_vol), 2),
            "prior_up_pct": round(100 * prior), "left_high": round(float(H0), 2),
            "footprint": f"{round((i - j0) / 5)}W {round(100 * depth)}% W",
            "pivot": round(pivot, 2), "trigger_ibd": round(pivot + 0.10, 2), "stop": round(stop, 2), "invalidation": round(float(Lb) * 0.995, 2),
            "risk_pct": round(100 * risk, 1), "dist_to_pivot_pct": round(100 * (c[-1] / pivot - 1), 1),
            "dry5": round(float(P.dry5[t].iloc[i]), 2) if not math.isnan(P.dry5[t].iloc[i]) else None, "checks": checks,
        }
    return None


# ----------------------------------------------------------------------------
# v4.6（07追補）: ポケットピボット（E10.1a。本文E10.1を改訂）
# ----------------------------------------------------------------------------
def detect_pocket_pivot(P, t, i, tt_row, stage):
    """E10.1a: 当日出来高が直前10営業日の下落日の最大出来高を上回って上昇し、10日線か50日線の近くにある日。"""
    c, h, l, v = P.C[t].values, P.H[t].values, P.L[t].values, P.V[t].values
    if i < 70 or np.isnan(c[i - 70: i + 1]).any() or np.isnan(v[i - 11: i + 1]).any():
        return None
    if not c[i] > c[i - 1]:
        return None
    downs = [j for j in range(i - PPV["lookback"], i) if c[j] < c[j - 1]]     # 同値日は含めない
    if not downs:
        return None                                                            # 下落日0件は unverified（出さない）
    vmax = max(v[j] for j in downs)
    if not (vmax > 0 and v[i] > vmax):
        return None
    ma = {"10": P.s10[t].values, "50": P.s50[t].values}
    near = {}
    for k, m in ma.items():
        if math.isnan(m[i]):
            continue
        lo_dev, c_dev = l[i] / m[i] - 1, c[i] / m[i] - 1
        if abs(lo_dev) <= PPV["near_low_pct"] / 100 and 0 <= c_dev <= PPV["max_above_ma_pct"] / 100:
            near[k] = abs(lo_dev)
    if not near:
        return None                                                            # 線から離れた大商いの上げ（延びすぎ）は対象外
    sup = min(near, key=near.get)
    m = ma[sup]
    excl = []
    if math.isnan(P.s50[t].values[i]) or c[i] < P.s50[t].values[i]:
        excl.append("50日線の下")
    if not (bool(tt_row.get("tt1")) and bool(tt_row.get("tt3"))):
        excl.append("上昇トレンドでない（150・200日線の下、または200日線が下向き）")
    if stage and stage.get("stage") == 4:
        excl.append("Stage4（下降）")
    dev = [l[j] / m[j] - 1 for j in range(i - PPV["lookback"], i) if not math.isnan(m[j])]
    if dev and min(dev) < -PPV["vshape_below_pct"] / 100:
        excl.append(f"V字（直前10日に{sup}日線を{PPV['vshape_below_pct']:.0f}%超割り込んでから急反発）")
    # ウェッジ：直前15日で+5%以上上げ、上げた日の出来高が下げた日より少なく、安値の傾きが高値の傾きより大きい（上向きに収束）
    n = PPV["wedge_days"]
    w = range(i - n, i)
    rise = c[i - 1] / c[i - n - 1] - 1
    upv = [v[j] for j in w if c[j] > c[j - 1]]
    dnv = [v[j] for j in w if c[j] < c[j - 1]]
    x = np.arange(n)
    sh_, sl_ = np.polyfit(x, h[i - n: i], 1)[0], np.polyfit(x, l[i - n: i], 1)[0]
    wedge = bool(rise >= PPV["wedge_rise_pct"] / 100 and upv and dnv and np.mean(upv) < np.mean(dnv) and sl_ > sh_ > 0)
    if wedge:
        excl.append("ウェッジ（出来高の少ない上げで上向きに収束した後）")
    stop = l[i] * 0.995
    risk = (c[i] - stop) / c[i]
    if risk > PPV["max_risk_pct"] / 100:
        excl.append(f"損切り幅{100 * risk:.1f}%（7%超）")
    lb = PPV["base_lookback"]
    jH = i - lb + int(np.nanargmax(h[i - lb: i]))
    H65 = h[jH]
    corr = (H65 - np.nanmin(l[jH: i])) / H65 if jH < i else 0.0
    if sup == "50":
        kind = "ma50"
    elif c[i] < H65 and corr >= PPV["base_corr"]:
        kind = "in_base"
    else:
        kind = "continuation"
    rng = h[i] - l[i]
    return {
        "pattern": "pocket_pivot", "ticker": t, "date": _d(P, i), "kind": kind, "support_ma": sup,
        "kind_ja": {"in_base": "ベースの中", "continuation": "ブレイク後の10日線沿い", "ma50": "50日線での反発"}[kind],
        "close": round(float(c[i]), 2), "low": round(float(l[i]), 2), "stop": round(float(stop), 2), "risk_pct": round(100 * risk, 1),
        "volume": int(v[i]), "max_down_volume": int(vmax), "vol_vs_max_down": round(float(v[i] / vmax), 2), "down_days": len(downs),
        "rvol50": round(float(P.rvol50[t].values[i]), 2) if not math.isnan(P.rvol50[t].values[i]) else None,
        "close_vs_ma_pct": round(100 * float(c[i] / m[i] - 1), 1), "low_vs_ma_pct": round(100 * float(l[i] / m[i] - 1), 1),
        "close_vs_10sma_pct": round(100 * float(c[i] / ma["10"][i] - 1), 1) if not math.isnan(ma["10"][i]) else None,
        "close_vs_50sma_pct": round(100 * float(c[i] / ma["50"][i] - 1), 1) if not math.isnan(ma["50"][i]) else None,
        "with_breakout": bool(c[i] > H65 >= c[i - 1]), "gap_up": bool(l[i] > h[i - 1]),
        "clv": round(float((c[i] - l[i]) / rng), 2) if rng > 0 else None,
        "wedge": wedge, "exclusions": excl, "qualified": not excl, "fundamentals": "unverified",
    }


# ----------------------------------------------------------------------------
# 売り検討シグナル（E13.1）
# ----------------------------------------------------------------------------
def sell_signals(P, t, i):
    c, h, l, v = P.C[t].iloc[: i + 1], P.H[t].iloc[: i + 1], P.L[t].iloc[: i + 1], P.V[t].iloc[: i + 1]
    if len(c) < 260 or c.iloc[-130:].isna().any():
        return None
    # 上昇開始＝直近130日の確認済みスイング安値のうち最も低いもの（簡易）
    sh, sl = swings(h.iloc[-135:], l.iloc[-135:])
    if not sl:
        return None
    off = len(c) - 135
    j_start = min(sl, key=lambda x: x[1])[0] + off
    run = c.iloc[j_start: i + 1]
    gain = c.iloc[-1] / c.iloc[j_start] - 1
    out = {"run_start": str(c.index[j_start].date()), "gain_from_start_pct": round(100 * gain, 1), "climax": [], "weakness": []}
    r = c.pct_change().iloc[j_start + 1: i + 1]
    if gain >= 0.25 and len(r) > 5:
        if r.iloc[-1] == r.max():
            out["climax"].append("最大の1日上昇率")
        wk = (h.rolling(5).max() - l.rolling(5).min()).iloc[j_start + 5: i + 1]
        if len(wk) and wk.iloc[-1] == wk.max():
            out["climax"].append("最大の週間値幅")
        if l.iloc[-1] > h.iloc[-2]:
            out["climax"].append("上放れギャップ")
        if (r.iloc[-10:] > 0).sum() >= 8:
            out["climax"].append("10日中8日以上の上昇")
        s200 = P.s200[t].iloc[i]
        ext = c.iloc[-1] / s200 - 1 if s200 and not math.isnan(s200) else 0
        hist = (P.C[t] / P.s200[t] - 1).iloc[-504:].dropna()
        if ext >= 0.70 or (len(hist) > 100 and ext >= hist.quantile(0.95)):
            out["climax"].append(f"200日線から+{round(100 * ext)}%の乖離")
        if v.iloc[-1] == v.iloc[j_start: i + 1].max() and r.iloc[-1] > 0:
            out["climax"].append("上昇開始以来の最大出来高")
    s50, e21 = P.s50[t].iloc[i], P.e21[t].iloc[i]
    if c.iloc[-1] < s50 <= c.iloc[-2] if not math.isnan(s50) else False:
        out["weakness"].append("50日線を終値で割り込み" + ("（出来高増）" if P.rvol50[t].iloc[i] >= 1.5 else ""))
    if len(r) > 5 and r.iloc[-1] == r.min() and v.iloc[-1] > v.iloc[-2]:
        out["weakness"].append("上昇開始以来の最大下落率（出来高増）")
    if not math.isnan(s50) and not math.isnan(e21) and c.iloc[-1] < min(s50, e21) <= c.iloc[-2] and P.rvol50[t].iloc[i] >= 1.5:
        out["weakness"].append("21EMAと50日線を同日に割り込み（出来高増）")
    out["level"] = "climax_alert" if len(out["climax"]) >= 3 else "climax_watch" if len(out["climax"]) == 2 else None
    return out


# ----------------------------------------------------------------------------
# v4.5 (A) 先導セクターの推移（E7.4）。観察専用。Tapeレジームには使わない
# ----------------------------------------------------------------------------
ETF_SECTOR_JA = {"XLK": "情報技術", "XLC": "通信", "XLY": "一般消費財", "XLP": "生活必需品", "XLE": "エネルギー", "XLF": "金融",
                 "XLV": "ヘルスケア", "XLI": "資本財", "XLB": "素材", "XLRE": "不動産", "XLU": "公益"}
SECTOR_JA = {"Information Technology": "情報技術", "Communication Services": "通信", "Consumer Discretionary": "一般消費財",
             "Consumer Staples": "生活必需品", "Energy": "エネルギー", "Financials": "金融", "Health Care": "ヘルスケア",
             "Industrials": "資本財", "Materials": "素材", "Real Estate": "不動産", "Utilities": "公益", "other": "その他"}
LEAD = {"tech_halved": 0.5, "top_n": 3, "list_max": 15}


def group_of(t, groups):
    g = groups.get(t, "")
    for k in GROUP_BENCH:
        if k in g:
            return k
    return None


def leadership_rotation(P, uni_stocks, groups, sector_of, leaders_now, leaders_4w, leaders_y, leaders_y4, rank_now, rs_now, rs_4w, i):
    """先導候補（市場全体）の分布の変化：セクター別・監視グループ別の数（今／4週前）、新規・脱落、セクターETFの強さの順位。
    key_event leadership_shift＝最多セクター上位3の顔ぶれが前日から変わった、またはテックの先導株数が4週前の半分を初めて割った。"""
    def counts(lst):
        d = {}
        for t in lst:
            s = sector_of(t)
            d[s] = d.get(s, 0) + 1
        return d
    now, w4 = counts(leaders_now), counts(leaders_4w)
    secs = sorted(set(now) | set(w4), key=lambda s: (-now.get(s, 0), s))
    by_sector = [{"sector": s, "sector_ja": SECTOR_JA.get(s, s), "now": now.get(s, 0), "w4_ago": w4.get(s, 0), "change": now.get(s, 0) - w4.get(s, 0)} for s in secs]
    top_now = [s for s in secs[:LEAD["top_n"]] if now.get(s, 0) > 0]
    y_counts = counts(leaders_y)
    top_y = [s for s in sorted(y_counts, key=lambda s: (-y_counts[s], s))[:LEAD["top_n"]] if y_counts[s] > 0]
    tech_now = [t for t in leaders_now if t in uni_stocks]
    tech_4w = [t for t in leaders_4w if t in uni_stocks]
    tech_y, tech_y4 = [t for t in leaders_y if t in uni_stocks], [t for t in leaders_y4 if t in uni_stocks]
    by_group = []
    for g in GROUP_BENCH:
        n_now, n_4w = sum(1 for t in tech_now if group_of(t, groups) == g), sum(1 for t in tech_4w if group_of(t, groups) == g)
        by_group.append({"group": g, "now": n_now, "w4_ago": n_4w, "change": n_now - n_4w,
                         "tickers": sorted([t for t in tech_now if group_of(t, groups) == g])})
    s4w = set(leaders_4w)
    new = [t for t in leaders_now if t not in s4w]
    dropped = [t for t in leaders_4w if t not in set(leaders_now)]
    def row(t, now_leader):
        return {"ticker": t, "sector": sector_of(t), "sector_ja": SECTOR_JA.get(sector_of(t), sector_of(t)), "in_universe": t in uni_stocks,
                "rs_rank": fnum(rank_now.get(t, np.nan)),
                "pct_from_52w_high": fnum(100 * (P.C[t].iloc[i] / P.h252[t].iloc[i] - 1)) if t in P.C.columns and not math.isnan(P.h252[t].iloc[i]) else None}
    new_rows = sorted((row(t, True) for t in new), key=lambda r: -(r["rs_rank"] or 0))
    drop_rows = sorted((row(t, False) for t in dropped), key=lambda r: (r["pct_from_52w_high"] if r["pct_from_52w_high"] is not None else 0))
    # セクターETFの市場比の強さの順位（3か月＝rel_63。4週前と比較）
    etfs = [e for e in SECTOR_ETFS if e in rs_now.index and e in rs_4w.index]
    r63 = {e: float(rs_now.loc[e, "rel_63"]) for e in etfs}
    r63p = {e: float(rs_4w.loc[e, "rel_63"]) for e in etfs}
    order_now = sorted(etfs, key=lambda e: -r63[e])
    order_4w = sorted(etfs, key=lambda e: -r63p[e])
    etf_rows = [{"etf": e, "sector_ja": ETF_SECTOR_JA.get(e, e), "rel_21_pct": fnum(100 * rs_now.loc[e, "rel_21"]), "rel_63_pct": fnum(100 * r63[e]),
                 "rank_63": order_now.index(e) + 1, "rank_63_4w_ago": order_4w.index(e) + 1,
                 "rank_change": (order_4w.index(e) + 1) - (order_now.index(e) + 1)} for e in order_now]
    halved_now = len(tech_4w) >= 4 and len(tech_now) <= LEAD["tech_halved"] * len(tech_4w)
    halved_y = len(tech_y4) >= 4 and len(tech_y) <= LEAD["tech_halved"] * len(tech_y4)
    events = []
    if set(top_now) != set(top_y) and top_y:
        events.append({"type": "leadership_shift", "kind": "top_sectors", "from": [SECTOR_JA.get(s, s) for s in top_y], "to": [SECTOR_JA.get(s, s) for s in top_now],
                       "counts": {SECTOR_JA.get(s, s): now.get(s, 0) for s in top_now}})
    if halved_now and not halved_y:
        events.append({"type": "leadership_shift", "kind": "tech_halved", "tech_now": len(tech_now), "tech_4w_ago": len(tech_4w),
                       "semi_now": next((g["now"] for g in by_group if g["group"] == "半導体"), None)})
    out = {"definition_version": "LR-1.0",
           "market": {"now": len(leaders_now), "w4_ago": len(leaders_4w), "top_sectors_now": [SECTOR_JA.get(s, s) for s in top_now],
                      "top_sectors_yesterday": [SECTOR_JA.get(s, s) for s in top_y]},
           "by_sector": by_sector,
           "tech": {"now": len(tech_now), "w4_ago": len(tech_4w), "change": len(tech_now) - len(tech_4w), "halved_vs_4w": bool(halved_now), "by_group": by_group},
           "new_leaders_vs_4w": new_rows[:LEAD["list_max"]], "new_count": len(new_rows),
           "dropped_leaders_vs_4w": drop_rows[:LEAD["list_max"]], "dropped_count": len(drop_rows),
           "sector_etf_rank": etf_rows,
           "note": "観察専用（E7.4）。先導候補の定義はE7（TT価格7項目＋市場RS百分位80以上＋SPY比63日>0）。"
                   "セクターはS&P500構成銘柄のGICS、監視母集団の非構成銘柄はIT扱い。Tapeレジーム・買い判定には使わない。"}
    return out, events


# ----------------------------------------------------------------------------
# v4.5 (B) ヘッジ・指数ショートの条件（C10.1）。自動でヘッジを設定しない。条件の成立だけを出す
# ----------------------------------------------------------------------------
HEDGE = {"rvol_min": 1.2, "near_high_pct": -10.0, "stop_ath_within_pct": 3.0, "lookback_hi": 10, "rally_min_days": 3}


def hedge_rules(P, chk, regime, idx_state, i):
    """ヘッジ（H1／H2）の価格セットアップ（P）。
    許可：レジームがC・D・F、または指数の崩れガード（IX3）成立。
    成立（SPY・QQQ各）：直近10日に3日以上の反発（終値が3日連続で前日より高い区間）があり、今日が
      21EMA または 50SMA の下抜け（前日は上）で、出来高が50日平均の1.2倍以上、52週高値から10%以内。
    損切り：52週高値が3%以内ならその高値、そうでなければ直近10日の高値。
    水準（setup_level）：D・Fで200SMAの下ならH2、それ以外はH1。D部の最終 hedge_level とは別。
    解除：FTD成立（30営業日以内・未失敗）またはレジームがA・B・Eに確定（損切り到達による解除は建玉を知らないので出さない）。"""
    allowed = regime in ("C", "D", "F") or bool(chk.get("IX3_index_breakdown_guard"))
    setups = []
    for s in ("SPY", "QQQ"):
        if s not in P.C.columns:
            continue
        c, cp = float(P.C[s].iloc[i]), float(P.C[s].iloc[i - 1])
        e21, e21p, s50, s50p = P.e21[s].iloc[i], P.e21[s].iloc[i - 1], P.s50[s].iloc[i], P.s50[s].iloc[i - 1]
        rv = P.rvol50[s].iloc[i]
        if any(math.isnan(x) for x in (e21, e21p, s50, s50p, rv)):
            continue
        cross21, cross50 = (c < e21 and cp >= e21p), (c < s50 and cp >= s50p)
        seg = P.C[s].iloc[i - HEDGE["lookback_hi"]:i + 1]
        ups = (seg.diff() > 0).astype(int)
        run, best = 0, 0
        for u in ups.iloc[1:]:
            run = run + 1 if u else 0
            best = max(best, run)
        rally = best >= HEDGE["rally_min_days"]
        near = chk[s]["pct_from_52w_high"] >= HEDGE["near_high_pct"]
        if allowed and (cross21 or cross50) and rv >= HEDGE["rvol_min"] and near and rally:
            h52 = float(P.h252[s].iloc[i])
            h10 = float(P.H[s].iloc[i - HEDGE["lookback_hi"] + 1:i + 1].max())
            stop = h52 if 100 * (c / h52 - 1) >= -HEDGE["stop_ath_within_pct"] else h10
            level = "H2" if (regime in ("D", "F") and not chk[s]["above_200"]) else "H1"
            setups.append({"instrument": s, "setup_level": level, "trigger": "21EMA割れ" if cross21 else "50日線割れ", "close": round(c, 2),
                           "stop": round(stop, 2), "stop_kind": "52週高値" if stop == h52 else "直近10日高値", "risk_to_stop_pct": fnum(100 * (stop / c - 1)),
                           "rvol50": fnum(rv, 2), "pct_from_52w_high": chk[s]["pct_from_52w_high"]})
    ftd_ok = any(idx_state[k]["ftd_date"] and not idx_state[k]["ftd_failed"] and (idx_state[k]["ftd_days_ago"] or 99) <= 30 for k in idx_state)
    cover = ftd_ok or regime in ("A", "B", "E")
    reasons = []
    if ftd_ok:
        reasons.append("FTD成立（30営業日以内）")
    if regime in ("A", "B", "E"):
        reasons.append(f"レジームが{regime}")
    level_max = "H2" if regime in ("D", "F") else ("H1" if allowed else "H0")
    status = "setup_today" if setups else ("allowed" if allowed else "not_allowed")
    return {"definition_version": "HG-1.0", "allowed": allowed, "status": status, "setup_level_max": level_max, "setups": setups,
            "cover_signal": bool(cover), "cover_reasons": reasons,
            "note": "C10.1（P）。ヘッジは自動設定しない。setup_level は条件がそろった場合の上限で、D部の最終 hedge_level とは別。損切りに達した場合の解除は自分で管理する。"}


# ----------------------------------------------------------------------------
# v4.5 (C) 長期SPY配分モデル（F10、P）。本人の長期モデルの代替（中身は非公開のため独自）。観察専用
# ----------------------------------------------------------------------------
SPYM = {"breadth_min": 50.0, "vix_max": 25.0, "buy_min": 60, "sell_max": 20, "confirm_days": 3, "hist_days": 300}


def spy_model(P, sectors, i):
    """5項目（各0/1、合計×20＝配分%）：(1) SPY終値>200SMA、(2) 200SMAが21日前より上、(3) S&P500構成銘柄の200SMA上比率>=50%、
    (4) 52週新高値−新安値の5日平均>0、(5) VIX<25 かつ HYG>50SMA。
    シグナル：60%以上が3日続けば buy、20%以下が3日続けば sell（ヒステリシス。過去1年の試算で往復を減らすため）。配分%は合計点そのもの。"""
    C = P.C
    if "SPY" not in C.columns:
        return {"status": "missing", "note": "SPYなし"}
    mem = [t for t in C.columns if t in sectors]
    lo = max(0, i - SPYM["hist_days"])
    idx = C.index[lo:i + 1]
    spy, s200 = C["SPY"].loc[idx], P.s200["SPY"].loc[idx]
    c1 = (spy > s200)
    c2 = (P.s200["SPY"].loc[idx] > P.s200["SPY"].shift(21).loc[idx])
    Cm, S200m = C[mem].loc[idx], P.s200[mem].loc[idx]
    valid = Cm.notna() & S200m.notna()
    pct200 = 100 * (Cm > S200m)[valid].sum(axis=1) / valid.sum(axis=1).replace(0, np.nan)
    c3 = pct200 >= SPYM["breadth_min"]
    Hm, Lm = P.H[mem].loc[idx], P.L[mem].loc[idx]
    h252, l252 = P.h252[mem].loc[idx], P.l252[mem].loc[idx]
    nh = ((Hm >= h252) & h252.notna()).sum(axis=1)
    nl = ((Lm <= l252) & l252.notna()).sum(axis=1)
    nhnl = (nh - nl).where(h252.notna().sum(axis=1) >= 0.8 * len(mem)).rolling(5).mean()
    c4 = nhnl > 0
    vix = C["^VIX"].loc[idx] if "^VIX" in C.columns else pd.Series(np.nan, index=idx)
    hyg = C["HYG"].loc[idx] if "HYG" in C.columns else pd.Series(np.nan, index=idx)
    hyg50 = P.s50["HYG"].loc[idx] if "HYG" in C.columns else pd.Series(np.nan, index=idx)
    c5 = (vix < SPYM["vix_max"]) & (hyg > hyg50)
    comps = pd.DataFrame({"spy_above_200": c1, "sma200_rising": c2, "breadth_200_ok": c3, "nhnl_positive": c4, "vix_credit_ok": c5})
    ok = pd.concat([s200, pct200, nhnl, vix, hyg50], axis=1).notna().all(axis=1)
    comps = comps[ok]
    if len(comps) < SPYM["confirm_days"] + 1:
        return {"status": "missing", "note": "履歴不足"}
    score = 20 * comps.astype(int).sum(axis=1)
    sig, changes = None, []
    for d, v in score.items():
        if sig is None:
            sig = "buy" if v >= SPYM["buy_min"] else "sell"
            changes.append({"date": str(d.date()), "signal": sig, "composite": int(v), "initial": True})
            continue
        recent = score.loc[:d].iloc[-SPYM["confirm_days"]:]
        if sig == "buy" and (recent <= SPYM["sell_max"]).all():
            sig = "sell"
            changes.append({"date": str(d.date()), "signal": "sell", "composite": int(v)})
        elif sig == "sell" and (recent >= SPYM["buy_min"]).all():
            sig = "buy"
            changes.append({"date": str(d.date()), "signal": "buy", "composite": int(v)})
    last = comps.index[-1]
    cur = {k: bool(comps.loc[last, k]) for k in comps.columns}
    prev_score = int(score.iloc[-2]) if len(score) >= 2 else None
    s20 = int(score.iloc[-21]) if len(score) >= 21 else None
    return {"definition_version": "SM-1.0", "status": "ok", "as_of": str(last.date()), "composite": int(score.iloc[-1]), "composite_1d_ago": prev_score,
            "composite_4w_ago": s20, "allocation_pct": int(score.iloc[-1]), "components": cur,
            "inputs": {"pct_above_200sma": fnum(pct200.loc[last]), "nhnl_5d": fnum(nhnl.loc[last]), "vix": fnum(vix.loc[last]),
                       "hyg_vs_50sma_pct": fnum(100 * (hyg.loc[last] / hyg50.loc[last] - 1))},
            "signal": sig, "signal_since": changes[-1]["date"], "signal_changes_recent": changes[-8:],
            "history_days": int(len(score)), "history_from": str(comps.index[0].date()),
            "note": "F10（P）。本人の長期SPYモデルの代替として独自に定義。観察専用で、Tapeレジーム・L/H・買い判定には使わない。"}


# ----------------------------------------------------------------------------
# v4.5b 判定結果の日次履歴（F2.2）。1日1行を data ブランチの history_signals.csv に追記（同じ基準日は上書き）
# ----------------------------------------------------------------------------
HIST_COLS = ["as_of", "generated_utc", "definition_version", "source",
             "regime_checklist", "regime_effective", "hysteresis", "provisional", "C_support_met",
             "spy_close", "spy_pct_from_52w_high", "spy_above_21ema", "spy_above_50", "spy_above_200",
             "qqq_close", "qqq_pct_from_52w_high", "qqq_above_21ema", "qqq_above_50", "qqq_above_200",
             "IX2", "IX3_guard", "ftd_valid", "BR1", "BR1_4w", "BR2", "BR2_4w", "BR3", "BR4", "BR4_label", "BR5_tech", "BR5_tech_label",
             "tech_pct_above_50sma", "megacap_led", "LD1", "LD1_4w", "LD1_sectors", "top_sectors", "tech_leaders", "tech_leaders_4w", "semi_leaders",
             "FB1_events", "FB1_d5_hold", "FB1_d5_ret", "FB1_d10_ret", "FB1_stalling",
             "hedge_allowed", "hedge_status", "hedge_setups", "hedge_cover",
             "spy_model_composite", "spy_model_signal", "spy_model_since",
             "soxx_close", "soxx_pct_from_52w_high", "vix_close",
             "setups_ready", "setups_watch", "key_events_n", "key_event_types",
             "setups_ready_standard", "pocket_pivots_n",   # v4.6
             "semi_stage2_pct", "semi_stage4_pct", "sp500_stage2_pct", "sp500_stage4_pct"]   # v4.6b


def history_row(tstr, gen, chk, eff, hyst, idx_state, lead, hedge, spym, P, i, setups, key_events, source="engine", sbreadth=None):
    """履歴1行。値は signals.json と同じ（再計算しない）。backfill では setups・key_events を空にする。"""
    g = lambda d, k: d.get(k) if isinstance(d, dict) else None
    ftd_ok = any(idx_state[k]["ftd_date"] and not idx_state[k]["ftd_failed"] and (idx_state[k]["ftd_days_ago"] or 99) <= 30 for k in idx_state)
    spy, qqq = chk["SPY"], chk["QQQ"]
    tech = lead["tech"]
    semi = next((x["now"] for x in tech["by_group"] if x["group"] == "半導体"), None)
    def px(t):
        return round(float(P.C[t].iloc[i]), 2) if t in P.C.columns else None
    def dd(t):
        return fnum(100 * (P.C[t].iloc[i] / P.h252[t].iloc[i] - 1)) if t in P.C.columns and not math.isnan(P.h252[t].iloc[i]) else None
    row = {"as_of": tstr, "generated_utc": gen, "definition_version": DEF_VERSION, "source": source,
           "regime_checklist": chk["regime"], "regime_effective": eff, "hysteresis": hyst, "provisional": chk["provisional"], "C_support_met": chk.get("C_support_met"),
           "spy_close": spy["close"], "spy_pct_from_52w_high": spy["pct_from_52w_high"], "spy_above_21ema": spy["above_21ema"], "spy_above_50": spy["above_50"], "spy_above_200": spy["above_200"],
           "qqq_close": qqq["close"], "qqq_pct_from_52w_high": qqq["pct_from_52w_high"], "qqq_above_21ema": qqq["above_21ema"], "qqq_above_50": qqq["above_50"], "qqq_above_200": qqq["above_200"],
           "IX2": chk["IX2"], "IX3_guard": chk.get("IX3_index_breakdown_guard"), "ftd_valid": ftd_ok,
           "BR1": chk.get("BR1_pct_above_50sma"), "BR1_4w": chk.get("BR1_4w_ago"), "BR2": chk.get("BR2_nhnl_5d"), "BR2_4w": chk.get("BR2_4w_ago"), "BR3": chk.get("BR3_rsp_spy_4w_pct"),
           "BR4": chk.get("BR4_pct_down20"), "BR4_label": chk.get("BR4_label"), "BR5_tech": chk.get("BR5_tech_pct_down20"), "BR5_tech_label": chk.get("BR5_tech_label"),
           "tech_pct_above_50sma": chk.get("BR5_tech_pct_above_50sma"), "megacap_led": chk.get("megacap_led"),
           "LD1": chk.get("LD1_leaders_market"), "LD1_4w": chk.get("LD1_4w_ago"), "LD1_sectors": len(chk.get("LD1_sectors") or {}),
           "top_sectors": "/".join(lead["market"]["top_sectors_now"]), "tech_leaders": tech["now"], "tech_leaders_4w": tech["w4_ago"], "semi_leaders": semi,
           "FB1_events": g(chk.get("FB1"), "events"), "FB1_d5_hold": g(chk.get("FB1"), "d5_hold_rate"), "FB1_d5_ret": g(chk.get("FB1"), "d5_avg_ret_pct"),
           "FB1_d10_ret": g(chk.get("FB1"), "d10_avg_ret_pct"), "FB1_stalling": chk.get("FB1_stalling"),
           "hedge_allowed": hedge["allowed"], "hedge_status": hedge["status"],
           "hedge_setups": ";".join(f"{x['instrument']}:{x['setup_level']}@{x['stop']}" for x in hedge["setups"]), "hedge_cover": hedge["cover_signal"],
           "spy_model_composite": spym.get("composite"), "spy_model_signal": spym.get("signal"), "spy_model_since": spym.get("signal_since"),
           "soxx_close": px("SOXX"), "soxx_pct_from_52w_high": dd("SOXX"), "vix_close": px("^VIX"),
           "setups_ready": (sum(1 for x in setups if x.get("status") == "ready") if setups is not None else None),
           "setups_watch": (sum(1 for x in setups if x.get("status") == "watch") if setups is not None else None),
           "key_events_n": (len(key_events) if key_events is not None else None),
           "key_event_types": ("/".join(sorted({e["type"] for e in key_events})) if key_events is not None else None),
           "setups_ready_standard": (len({x["ticker"] for x in setups if x.get("status") == "ready" and x.get("setup_type") == "standard"})
                                     if setups is not None else None),
           "pocket_pivots_n": (sum(1 for e in key_events if e.get("type") == "pocket_pivot") if key_events is not None else None),
           "semi_stage2_pct": ((sbreadth or {}).get("監視：半導体") or {}).get("pct_stage", {}).get(2),
           "semi_stage4_pct": ((sbreadth or {}).get("監視：半導体") or {}).get("pct_stage", {}).get(4),
           "sp500_stage2_pct": ((sbreadth or {}).get("S&P500全体") or {}).get("pct_stage", {}).get(2),
           "sp500_stage4_pct": ((sbreadth or {}).get("S&P500全体") or {}).get("pct_stage", {}).get(4)}
    return {k: row.get(k) for k in HIST_COLS}


def write_history(out, prev_path, seed_path, row):
    """前回の履歴（data ブランチ）に今日の行を追記して保存。無ければ main の seed（backfill）から始める。同じ基準日は上書き。"""
    src = prev_path if prev_path and os.path.exists(prev_path) and os.path.getsize(prev_path) > 0 else (seed_path if seed_path and os.path.exists(seed_path) else None)
    hist = pd.read_csv(src, dtype=str) if src else pd.DataFrame(columns=HIST_COLS)
    hist = hist.reindex(columns=HIST_COLS)
    hist = hist[hist["as_of"] != row["as_of"]]
    hist = pd.concat([hist, pd.DataFrame([row])], ignore_index=True).sort_values("as_of")
    hist.to_csv(os.path.join(out, "history_signals.csv"), index=False)
    return len(hist), src


# ----------------------------------------------------------------------------
# Tapeチェックリスト（C15.1 改訂版）
# ----------------------------------------------------------------------------
def checklist(P, bw, leaders_now, leaders_4w, leader_sectors, fb, idx_state, i, bx=None):
    C = P.C
    items = {}
    for s in ("SPY", "QQQ"):
        c, s50, s200, s200p, e21 = C[s].iloc[i], P.s50[s].iloc[i], P.s200[s].iloc[i], P.s200[s].iloc[i - 21], P.e21[s].iloc[i]
        items[s] = {
            "close": round(float(c), 2), "above_21ema": bool(c > e21), "above_50": bool(c > s50), "above_200": bool(c > s200),
            "ma200_rising": bool(s200 > s200p), "IX1": bool(c > s50 > s200 and s200 > s200p),
            "IX1b": bool(c > s200 and s200 > s200p), "pct_from_52w_high": round(100 * float(c / P.h252[s].iloc[i] - 1), 1),
            "ma200_falling": bool(s200 < s200p),
        }
    items["IX2"] = max(idx_state["^GSPC"]["distribution_count"], idx_state["^IXIC"]["distribution_count"])
    b = breadth_items(bw, P.dates[i])
    items.update(b)
    br1, br1p = b.get("BR1_pct_above_50sma"), b.get("BR1_4w_ago")
    br2, br2p = b.get("BR2_nhnl_5d"), b.get("BR2_4w_ago")
    rsp = (C["RSP"] / C["SPY"]) if "RSP" in C.columns else None
    items["BR3_rsp_spy_4w_pct"] = round(100 * float(rsp.iloc[i] / rsp.iloc[i - 20] - 1), 2) if rsp is not None else None
    items["LD1_leaders_market"] = len(leaders_now)
    items["LD1_4w_ago"] = len(leaders_4w)
    items["LD1_sectors"] = leader_sectors
    items["FB1"] = fb
    ftd_ok = any(idx_state[k]["ftd_date"] and not idx_state[k]["ftd_failed"] and idx_state[k]["ftd_days_ago"] <= 30 for k in ("^GSPC", "^IXIC"))
    spy, qqq = items["SPY"], items["QQQ"]
    fb5 = fb.get("d5_hold_rate")
    fb_ok = fb.get("d5_matured", 0) >= 20
    # v4.4 (1) ブレイクの伸び
    fq = fb_quality(fb)
    fb_stall = fq["stalling"]
    items["FB1_extends"], items["FB1_stalling"] = fq["extends"], fb_stall
    # v4.4 (2) 市場内部の拡張（表示用の要約。Cの支持に使うのは S&P500 の隠れ弱気だけ）
    g = (bx or {}).get("groups", {})
    sp, tg = g.get("sp500", {}), g.get("tech", {})
    items["BR4_pct_down20"], items["BR4_4w_ago"], items["BR4_label"] = sp.get("pct_down20"), sp.get("pct_down20_4w_ago"), sp.get("label")
    items["BR5_tech_pct_down20"], items["BR5_tech_4w_ago"], items["BR5_tech_label"] = tg.get("pct_down20"), tg.get("pct_down20_4w_ago"), tg.get("label")
    items["BR5_tech_pct_above_50sma"] = tg.get("pct_above_50sma")
    items["megacap_led"] = (bx or {}).get("megacap_led")
    # v4.4 (3) 指数の崩れガード IX3
    guard = bool((not spy["above_50"] or not qqq["above_50"]) and items["IX2"] >= BRX["guard_dist_min"])
    items["IX3_index_breakdown_guard"] = guard
    unverified = []
    if br1 is None or br2 is None:
        unverified.append("breadth")
    # 判定（F → C → D → E → A → B）
    regime, reasons = None, []
    f_core = (spy["ma200_falling"] and not spy["above_200"]) or (qqq["ma200_falling"] and not qqq["above_200"])
    f_sup = sum([items["IX2"] >= 6, (br1 or 100) < 30, (br2 or 1) < 0, len(leaders_now) == 0 or len(leaders_now) < len(leaders_4w), (fb5 or 100) < 30])
    if f_core and f_sup >= 2:
        regime, reasons = "F", ["下降中の200日線下", f"支持{f_sup}件"]
    # Cの支持項目の充足数は、F判定の日も表示用に出す（手順書は毎回書く前提）
    c_core = spy["pct_from_52w_high"] >= -10 or qqq["pct_from_52w_high"] >= -10
    c_sup = sum([items["IX2"] >= 5, br1 is not None and br1p is not None and br1 <= br1p - 10, br2 is not None and br2p is not None and br2 < 0 and br2 < br2p,
                 (fb5 or 100) < 40 or fb_stall,                          # v4.4: 抜けても伸びない場合も数える
                 (items["BR3_rsp_spy_4w_pct"] or 0) <= -2,
                 items["BR4_label"] == "stealth"])                       # v4.4: 指数は高値圏なのに過半が−20%以上
    items["C_support_met"] = int(c_sup)
    if regime is None:
        if c_core and guard:
            who = "大型株主導の崩れ" if items["megacap_led"] else "指数の崩れ"
            regime, reasons = "C", ["高値圏", f"{who}（SPY/QQQの50日線割れ＋分配日{items['IX2']}）", f"分配・内部悪化の支持{c_sup}件"]
        elif c_core and c_sup >= 2:
            regime, reasons = "C", ["高値圏", f"分配・内部悪化の支持{c_sup}件"]
    e_ftd = ftd_ok and spy["above_21ema"] and spy["above_50"] and qqq["above_21ema"] and qqq["above_50"]
    recov_items = [br1 is not None and br1p is not None and br1 >= br1p + 15,
                   br2 is not None and br2p is not None and br2 > 0 and br2 > br2p,
                   len(leaders_now) >= len(leaders_4w),
                   (items["BR3_rsp_spy_4w_pct"] or 0) > 0]
    # v4.4 (3): 指数の崩れガードが出ている間は、内部回復入口でEにしない
    e_recov = spy["above_200"] and qqq["above_200"] and spy["ma200_rising"] and qqq["ma200_rising"] and sum(recov_items) >= 2 and not guard
    items["E_recovery_items_met"] = int(sum(recov_items))
    e_sup = sum([br1 is not None and br1p is not None and br1 >= br1p + 10, len(leaders_now) > len(leaders_4w), (items["BR3_rsp_spy_4w_pct"] or 0) > 0,
                 fb.get("d5_hold_rate") is not None and fb5 >= 50 and not fb_stall])  # v4.4: 伸びないブレイクは支持に数えない
    if regime is None:
        d_core = spy["pct_from_52w_high"] < -10 or qqq["pct_from_52w_high"] < -10 or not spy["above_200"] or not qqq["above_200"]
        if d_core and not (e_ftd or e_recov):
            regime, reasons = "D", ["調整中または200日線下、回復未確認"]
    if regime is None and ((e_ftd and e_sup >= 1) or e_recov) and not (spy["IX1"] and qqq["IX1"]):
        regime, reasons = "E", ["FTD成立" if e_ftd else f"FTDなしの内部回復（回復項目{sum(recov_items)}/4）", f"支持{e_sup}件"]
    a_core = (spy["IX1"] and qqq["IX1"] and items["IX2"] <= 3 and br1 is not None and br1 >= 60 and br1 >= (br1p or 0) and (br2 or -1) > 0
              and len(leaders_now) >= len(leaders_4w) and len(leaders_now) >= 10 and len(leader_sectors) >= 3 and fb_ok and fb5 >= 50
              and not fb_stall)  # v4.4
    if regime is None and a_core:
        regime, reasons = "A", ["指数配置・内部・先導・追随率がすべて充足"]
    if regime is None and ((spy["IX1b"] and spy["above_50"]) or (qqq["IX1b"] and qqq["above_50"])) and items["IX2"] <= 5 and len(leaders_now) >= 1:
        regime, reasons = "B", ["一方の指数が上向き200日線上かつ50日線上、先導あり、Aの条件未達"]
    if regime is None and (e_ftd or e_recov):
        regime, reasons = "E", ["回復条件は成立、支持条件が不足（provisional）"]
    provisional = regime is None or bool(unverified) or (regime == "A" and not fb_ok)
    if regime is None:
        regime, reasons = "Transition", ["どのレジームの必須条件も満たさない"]
    items["regime"] = regime
    items["regime_reasons"] = reasons
    items["provisional"] = provisional
    items["unverified"] = unverified
    return items


RANK = {"F": 0, "D": 1, "C": 2, "Transition": 2, "E": 3, "B": 4, "A": 5}


def apply_hysteresis(today, yesterday, prev_confirmed):
    """C15.2: 攻勢側への変更は2日連続で確定。防御側は即時。"""
    r = today["regime"]
    if prev_confirmed is None:
        return r, "initial"
    if RANK.get(r, 2) <= RANK.get(prev_confirmed, 2):
        return r, "confirmed"
    if yesterday is not None and yesterday.get("regime") == r:
        return r, "confirmed"
    return prev_confirmed, f"provisional→{r}（2日連続の確認待ち）"


# ----------------------------------------------------------------------------
# 台帳（E14）
# ----------------------------------------------------------------------------
def update_ledger(ledger, P, universe_setups, today, i):
    """前回の台帳にあるセットアップのブレイク/スクワット/失敗を今日の値で更新し、新規を登録する。"""
    setups = ledger.get("setups", [])
    tstr = str(today.date())
    by_key = {(s["ticker"], s["pattern"], s["pivot"]): s for s in setups}
    events = []
    C, H, L = P.C, P.H, P.L
    for s in setups:
        t = s["ticker"]
        if t not in C.columns or s.get("status") in ("failed", "closed", "expired", "superseded"):
            continue
        if s.get("registered", "") >= tstr:
            continue  # 今日の足で登録したものは、翌営業日から判定する（同日再実行での誤検出を防ぐ）
        c, h, l = C[t].iloc[i], H[t].iloc[i], L[t].iloc[i]
        if any(math.isnan(x) for x in (c, h, l)):
            continue
        Pv, Sv = s["pivot"], s["stop"]
        if s.get("breakout_date") is None:
            if c > Pv and C[t].iloc[i - 1] <= Pv:
                s["breakout_date"], s["breakout_close"] = tstr, round(float(c), 2)
                s["rvol50"] = round(float(P.rvol50[t].iloc[i]), 2) if not math.isnan(P.rvol50[t].iloc[i]) else None
                s["volume_confirmed"] = bool(s["rvol50"] and s["rvol50"] >= 1.5)
                rng = h - l
                s["weak_breakout"] = bool(rng > 0 and (c - l) / rng < 0.25)
                s["observation"] = "prospective" if s["registered"] < tstr else "retrospective"
                s["status"] = "breakout"
                events.append({"type": "breakout", "ticker": t, "pattern": s["pattern"], "pivot": Pv, "close": round(float(c), 2),
                               "rvol50": s["rvol50"], "weak": s["weak_breakout"]})
            elif h > Pv and c <= Pv:
                s["squat_dates"] = s.get("squat_dates", []) + [tstr]
                events.append({"type": "squat", "ticker": t, "pattern": s["pattern"], "pivot": Pv, "close": round(float(c), 2)})
            elif l <= s.get("invalidation", Sv):
                s["status"] = "failed"
                events.append({"type": "setup_failed", "ticker": t, "pattern": s["pattern"], "level": s.get("invalidation", Sv), "stop": Sv})
            elif (pd.Timestamp(tstr) - pd.Timestamp(s["registered"])).days > 45:
                s["status"] = "expired"
        else:
            jD = C.index.get_loc(pd.Timestamp(s["breakout_date"]))
            k = i - jD
            if k <= 0:
                continue
            if l <= Sv and not s.get("stop_touched"):
                s["stop_touched"], s["stop_touched_day"] = True, k
                events.append({"type": "stop_touched", "ticker": t, "pattern": s["pattern"], "day": k})
            if c < Pv and not s.get("pivot_failed"):
                s["pivot_failed"], s["pivot_failed_day"] = True, k
                events.append({"type": "pivot_failure", "ticker": t, "pattern": s["pattern"], "day": k})
            for kk in (3, 5, 10):
                if k == kk:
                    s[f"d{kk}"] = {"close": round(float(c), 2), "end_pivot_held": bool(c >= Pv), "close_profitable": bool(c > s["breakout_close"]),
                                   "ret_pct": round(100 * float(c / s["breakout_close"] - 1), 2), "stop_touched": bool(s.get("stop_touched", False)),
                                   "pivot_failed": bool(s.get("pivot_failed", False))}
            if k >= 10:
                s["status"] = "closed"
    for u in universe_setups:
        key = (u["ticker"], u["pattern"], u["pivot"])
        if u["status"] != "ready":
            continue
        same = [s for s in setups if s["ticker"] == u["ticker"] and s["pattern"] == u["pattern"] and s.get("status") in ("registered", "breakout")]
        if any(abs(s["pivot"] - u["pivot"]) / u["pivot"] < 0.015 for s in same):
            continue  # 同じセットアップの再登録はしない（ピボット差1.5%未満）
        if u["pattern"] in STANDARD_PATTERNS and any(
                s["ticker"] == u["ticker"] and s["pattern"] in STANDARD_PATTERNS and s["pattern"] != u["pattern"]
                and s.get("status") in ("registered", "breakout") and abs(s["pivot"] - u["pivot"]) / u["pivot"] < 0.015 for s in setups):
            continue  # v4.6c：同じベースを別の型で二重に登録しない（E14.1。先に登録した型に任せる。signals.setups の also に型名が残る）
        for s in same:
            if s.get("status") == "registered":
                s["status"] = "superseded"  # 形が変わった。新しい登録に置き換える
        if key not in by_key:
            rec = {"setup_id": f"{u['ticker']}-{u['pattern']}-{tstr}", "ticker": u["ticker"], "pattern": u["pattern"], "registered": tstr,
                   "pivot": u["pivot"], "stop": u["stop"], "risk_pct": u["risk_pct"], "status": "registered", "breakout_date": None,
                   "setup_type": u.get("setup_type"), "base_count": u.get("base_count"),   # v4.6（E14.1）
                   "invalidation": u.get("invalidation", u["stop"]),                         # v4.6c：ブレイク前の無効化水準（フラットベース＝ベース安値、ダブルボトム＝二番底）
                   "definition_version": DEF_VERSION}
            setups.append(rec)
            by_key[key] = rec
            events.append({"type": "new_ready", "ticker": u["ticker"], "pattern": u["pattern"], "pivot": u["pivot"], "stop": u["stop"], "risk_pct": u["risk_pct"],
                           "setup_type": u.get("setup_type"), "base_count": u.get("base_count"), "base_count_status": u.get("base_count_status")})
    # 古いものを畳む（直近120営業日より前のclosed/failed/expiredは集計だけ残す）
    cutoff = str((today - pd.tseries.offsets.BDay(120)).date())
    keep, folded = [], ledger.get("folded_counts", {})
    for s in setups:
        if s.get("status") in ("closed", "failed", "expired", "superseded") and s["registered"] < cutoff:
            folded[s["status"]] = folded.get(s["status"], 0) + 1
        else:
            keep.append(s)
    ledger["setups"], ledger["folded_counts"] = keep, folded
    return events


def register_pocket_pivots(ledger, pps, tstr, P, i):
    """E10.1a・E14: 適格なポケットピボットを当日をDとして台帳に登録（同じ銘柄は5営業日以内に重ねない）。"""
    setups = ledger.setdefault("setups", [])
    since = str(P.dates[max(0, i - PPV["dedupe_days"])].date())
    events = []
    for pp in pps:
        recent = [s for s in setups if s["ticker"] == pp["ticker"] and s["pattern"] == "pocket_pivot"
                  and (s.get("breakout_date") or "") > since and s.get("breakout_date") != tstr]
        same = [s for s in setups if s["ticker"] == pp["ticker"] and s["pattern"] == "pocket_pivot" and s.get("breakout_date") == tstr]
        pp["repeat_within_5d"] = bool(recent)
        if not recent and not same:
            setups.append({"setup_id": f"{pp['ticker']}-pocket_pivot-{tstr}", "ticker": pp["ticker"], "pattern": "pocket_pivot",
                           "kind": pp["kind"], "registered": tstr, "pivot": pp["low"], "stop": pp["stop"], "risk_pct": pp["risk_pct"],
                           "status": "breakout", "breakout_date": tstr, "breakout_close": pp["close"], "rvol50": pp["rvol50"],
                           "volume_confirmed": True, "weak_breakout": bool(pp["clv"] is not None and pp["clv"] < 0.25),
                           "with_breakout": pp["with_breakout"], "observation": "same_day", "setup_type": "pocket_pivot",
                           "base_count": pp.get("base_count"), "definition_version": DEF_VERSION,
                           "note": "D＝シグナル当日。pivot＝当日安値（終値がこれを割ればピボット失敗）、基準リターンは当日終値から"})
        events.append({"type": "pocket_pivot", "ticker": pp["ticker"], "kind": pp["kind"], "kind_ja": pp["kind_ja"], "support_ma": pp["support_ma"],
                       "close": pp["close"], "stop": pp["stop"], "risk_pct": pp["risk_pct"], "vol_vs_max_down": pp["vol_vs_max_down"],
                       "rvol50": pp["rvol50"], "with_breakout": pp["with_breakout"], "gap_up": pp["gap_up"],
                       "base_count": pp.get("base_count"), "repeat_within_5d": pp["repeat_within_5d"]})
    return events


def profit_tracking(ledger, P, i):
    """E13.2: ブレイク（PPはシグナル日）から60営業日まで、利益確定の目安（+20〜25%）と8週ルールを追う。自動売却ではない。"""
    events, rows = [], []
    for s in ledger.get("setups", []):
        bd = s.get("breakout_date")
        t = s["ticker"]
        if bd is None or t not in P.C.columns or pd.Timestamp(bd) not in P.dates:
            continue
        jD = P.dates.get_loc(pd.Timestamp(bd))
        k = i - jD
        if k < 1 or k > PT["track_days"] or s.get("stop_touched"):
            continue
        c = P.C[t].values
        ref = s["breakout_close"] if s["pattern"] == "pocket_pivot" else s["pivot"]   # 買いの基準＝ピボット（PPは当日終値）
        if not ref or math.isnan(c[i]):
            continue
        gain = 100 * (c[i] / ref - 1)
        mx = 100 * (np.nanmax(c[jD + 1: i + 1]) / ref - 1)
        early = c[jD + 1: min(i, jD + PT["eight_week_window"]) + 1]
        ew = s.get("eight_week") or {}
        if not ew and len(early) and 100 * (np.nanmax(early) / ref - 1) >= PT["eight_week_gain"]:
            j_hit = jD + 1 + int(np.nanargmax(early >= ref * (1 + PT["eight_week_gain"] / 100)))
            until = P.dates[jD] + pd.tseries.offsets.BDay(PT["eight_week_hold"])
            ew = {"active": True, "hit_date": _d(P, j_hit), "hit_day": int(j_hit - jD), "until": str(until.date())}
            s["eight_week"] = ew
            events.append({"type": "eight_week_hold", "ticker": t, "pattern": s["pattern"], "gain_pct": round(gain, 1),
                           "hit_day": ew["hit_day"], "until": ew["until"]})
        if ew.get("active") and str(P.dates[i].date()) >= ew["until"]:
            ew["active"] = False
            s["eight_week"] = ew
            events.append({"type": "eight_week_end", "ticker": t, "pattern": s["pattern"], "gain_pct": round(gain, 1)})
        if gain >= PT["zone_lo"] and not s.get("profit_zone_date") and not ew.get("active"):
            s["profit_zone_date"] = _d(P, i)
            events.append({"type": "profit_zone", "ticker": t, "pattern": s["pattern"], "ref": ref, "close": round(float(c[i]), 2),
                           "gain_pct": round(gain, 1), "days_since_breakout": int(k)})
        s50 = P.s50[t].values[i]
        rows.append({"ticker": t, "pattern": s["pattern"], "setup_type": s.get("setup_type"), "breakout_date": bd, "days": int(k),
                     "ref": ref, "close": round(float(c[i]), 2), "gain_pct": round(gain, 1), "max_gain_pct": round(float(mx), 1),
                     "zone": "20-25" if PT["zone_lo"] <= gain <= PT["zone_hi"] else ("over_25" if gain > PT["zone_hi"] else None),
                     "eight_week_active": bool(ew.get("active")), "eight_week_until": ew.get("until"),
                     "below_50sma": bool(not math.isnan(s50) and c[i] < s50), "pivot_failed": bool(s.get("pivot_failed"))})
    return events, sorted(rows, key=lambda r: -r["gain_pct"])


def ledger_cohorts(ledger):
    """E14.3: prospectiveのD+3/5/10集計（setup_type別）。PPはシグナル当日をDとする same_day。"""
    out = {}
    for s in ledger.get("setups", []):
        if s.get("breakout_date") is None or s.get("observation") not in ("prospective", "same_day"):
            continue
        g = out.setdefault(s["pattern"], {})
        for k in (3, 5, 10):
            d = s.get(f"d{k}")
            if not d:
                continue
            a = g.setdefault(f"d{k}", {"matured": 0, "end_pivot_held": 0, "close_profitable": 0, "stop_touched": 0, "follow_through": 0})
            a["matured"] += 1
            a["end_pivot_held"] += d["end_pivot_held"]
            a["close_profitable"] += d["close_profitable"]
            a["stop_touched"] += d["stop_touched"]
            a["follow_through"] += int(d["end_pivot_held"] and d["close_profitable"] and not d["stop_touched"])
    return out


# ----------------------------------------------------------------------------
# E7.1 セクター相対強度（v4.3、P）
# ----------------------------------------------------------------------------
def fnum(x, nd=1):
    """NaN → None、それ以外は丸める（JSON・CSV出力用）。"""
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    v = round(x, nd)
    return 0.0 if v == 0 else v


def sector_rs(P, stocks, bench_of, i):
    """銘柄ごとの対セクターETF相対強度（Rel_sec_21/63、52週高値距離の差、RSライン63本高値）と区分。
    区分（P）：3条件すべて正＝leader、すべて負＝laggard、それ以外＝neutral。"""
    C = P.C
    rows = []
    for t in stocks:
        b = bench_of(t)
        if not b or b not in C.columns or t not in C.columns:
            rows.append({"ticker": t, "sector_bench": b, "rel_sec_21_pct": None, "rel_sec_63_pct": None,
                         "dd_vs_sector_pt": None, "sector_rs_line_63h": None, "sector_rs_class": None})
            continue
        c, bc = C[t], C[b]
        rel = {}
        for k in (21, 63):
            if i - k < 0 or any(math.isnan(v) for v in (c.iloc[i], c.iloc[i - k], bc.iloc[i], bc.iloc[i - k])):
                rel[k] = float("nan")
            else:
                rel[k] = ((c.iloc[i] / c.iloc[i - k]) / (bc.iloc[i] / bc.iloc[i - k]) - 1) * 100
        dd_t, dd_b = c.iloc[i] / P.h252[t].iloc[i] - 1, bc.iloc[i] / P.h252[b].iloc[i] - 1
        dd_diff = (dd_t - dd_b) * 100 if not (math.isnan(dd_t) or math.isnan(dd_b)) else float("nan")
        line = (c / bc).iloc[max(0, i - 63):i + 1]
        line_hi = bool(line.iloc[-1] >= line.iloc[:-1].max()) if len(line) == 64 and line.notna().all() else None
        vals = (rel[21], rel[63], dd_diff)
        if any(math.isnan(v) for v in vals):
            cls = None
        elif all(v > 0 for v in vals):
            cls = "leader"
        elif all(v < 0 for v in vals):
            cls = "laggard"
        else:
            cls = "neutral"
        rows.append({"ticker": t, "sector_bench": b, "rel_sec_21_pct": fnum(rel[21]), "rel_sec_63_pct": fnum(rel[63]),
                     "dd_vs_sector_pt": fnum(dd_diff), "sector_rs_line_63h": line_hi, "sector_rs_class": cls})
    return pd.DataFrame(rows).set_index("ticker")


# ----------------------------------------------------------------------------
# E7.2 調整耐性（v4.3、P）
# ----------------------------------------------------------------------------
def _episode(c, ih, il, i, state):
    """調整窓の辞書。ih/il/i は c（Series）の位置。"""
    dd_low = (c.iloc[il] / c.iloc[ih] - 1) * 100
    dd_now = (c.iloc[i] / c.iloc[ih] - 1) * 100
    valid = dd_low <= RES["min_depth"] and (il - ih) >= RES["min_bars"]
    note = []
    if valid and dd_low > RES["shallow"]:
        note.append("浅い調整（参考）")
    return {"state": state, "high_date": str(c.index[ih].date()), "low_date": str(c.index[il].date()),
            "dd_low_pct": round(dd_low, 1), "dd_now_pct": round(dd_now, 1), "bars_high_to_low": int(il - ih),
            "bars_since_low": int(i - il), "valid": bool(valid), "note": "、".join(note),
            "_ih": ih, "_il": il}


def correction_window(P, bench, i):
    """基準ETFの調整窓（E7.2.2）。
    現在の窓：直近252本の終値最高値の日 T_high → 以降の最安値の日 T_low。
    状態：調整中（dd_now≦−5%）／回復中（それ以外で新高値未更新）。
    現在の窓が未成立（dd_low>−5%、または5本未満）なら、直近60本以内に新高値で完了した調整を探して「完了（直前）」。
    どれもなければ「なし」。"""
    base = {"bench": bench, "state": "なし", "valid": False, "high_date": None, "low_date": None,
            "dd_low_pct": None, "dd_now_pct": None, "bars_high_to_low": None, "bars_since_low": None, "note": ""}
    if bench not in P.C.columns:
        return {**base, "note": "基準ETFの価格なし"}
    c = P.C[bench].iloc[:i + 1]
    if c.notna().sum() < 130:
        return {**base, "note": "基準ETFの履歴不足"}
    ii = len(c) - 1
    seg = c.iloc[max(0, ii - 251):ii + 1]
    ih = int(c.index.get_loc(seg.idxmax()))
    il = int(c.index.get_loc(c.iloc[ih:ii + 1].idxmin()))
    cur = _episode(c, ih, il, ii, "調整中" if (c.iloc[ii] / c.iloc[ih] - 1) * 100 <= RES["min_depth"] else "回復中")
    if cur["valid"]:
        return {**base, **cur}
    # 完了した調整を後ろから探す（新高値日のあいだの谷）。
    rm = c.rolling(252, min_periods=1).max().shift(1)
    nh = (c >= rm).to_numpy()
    nh_idx = [k for k in range(max(1, ii - 300), ii + 1) if nh[k]]
    # 連続する新高値の塊（run）に分け、run と run の間を1つの調整エピソードとみなす
    runs = []
    for k in nh_idx:
        if runs and k == runs[-1][1] + 1:
            runs[-1][1] = k
        else:
            runs.append([k, k])
    for r in range(len(runs) - 1, 0, -1):
        peak = runs[r - 1][1]      # 前の run の最終日＝山
        rec = runs[r][0]           # 次の run の初日＝新高値で回復した日
        if ii - rec > RES["keep_bars"]:
            break
        if rec - peak < 2:
            continue
        trough = int(c.index.get_loc(c.iloc[peak:rec].idxmin()))
        ep = _episode(c, peak, trough, ii, "完了（直前）")
        if ep["valid"]:
            ep["note"] = "、".join(x for x in (ep["note"], f"新高値回復 {str(c.index[rec].date())}") if x)
            return {**base, **ep}
    note = f"現在の高値から {cur['dd_low_pct']}%（{cur['high_date']}→{cur['low_date']}）で調整未成立"
    return {**base, "note": note, "_cur": cur}


def resilience(P, stocks, w, i):
    """銘柄ごとの調整耐性（E7.2.3〜E7.2.5）。w は correction_window の戻り値（valid のもの）。"""
    cols = ["own_dd_low_pct", "r_held", "held_class", "own_dd_now_pct", "r_now", "rebound_class",
            "new_high_after_low", "below_50sma_days", "tier", "late_break"]
    empty = pd.DataFrame(index=pd.Index(stocks, name="ticker"), columns=cols)
    if not w.get("valid"):
        return empty
    ih, il = w["_ih"], w["_il"]
    dd_low, dd_now = w["dd_low_pct"] / 100, w["dd_now_pct"] / 100
    C, H, S50 = P.C, P.h252c, P.s50
    rows = []
    for t in stocks:
        if t not in C.columns:
            rows.append({"ticker": t, **{k: None for k in cols}})
            continue
        c = C[t]
        hist = int(c.iloc[:ih].notna().sum())
        own_now = c.iloc[i] / H[t].iloc[i] - 1 if not math.isnan(H[t].iloc[i]) else float("nan")
        r_now = own_now / dd_now if (dd_now * 100 < RES["now_na_above"] and not math.isnan(own_now)) else float("nan")
        after = c.iloc[il + 1:i + 1]
        newhi = bool((after >= H[t].shift(1).iloc[il + 1:i + 1]).any()) if len(after) else False
        if hist < RES["min_history"] or math.isnan(H[t].iloc[ih]):
            held, r_held, own_low, below = None, float("nan"), float("nan"), None
        else:
            win = c.iloc[ih:il + 1]
            h0 = max(H[t].iloc[ih], win.max())
            own_low = win.min() / h0 - 1
            r_held = own_low / dd_low
            held = "耐えた" if r_held <= RES["held_max"] else "同調" if r_held <= RES["inline_max"] else "崩れた"
            s = S50[t].iloc[ih:il + 1]
            below = int((win < s).sum()) if s.notna().all() else None
        # 戻り区分：R_now が主。新高値フラグは「いまも高値圏（−15%以内）」のときだけ同調→先行に格上げする
        # （何か月も前に新高値を付けてから崩れた銘柄を先行にしないため）。基準が高値圏で R_now が N/A のときは自分の高値距離で判定。
        if math.isnan(own_now):
            reb = None
        elif not math.isnan(r_now):
            reb = "先行" if r_now <= RES["held_max"] else "同調" if r_now <= RES["inline_max"] else "遅れ"
            if reb == "同調" and newhi and own_now >= RES["newhigh_upgrade_dd"]:
                reb = "先行"
        else:
            reb = "先行" if own_now >= -0.05 else "遅れ" if own_now <= -0.10 else "同調"
        tier = None
        if held == "耐えた" and reb in ("先行", "同調"):
            tier = "A"
        elif held == "同調" and reb == "先行":
            tier = "B"
        elif held == "崩れた" and reb == "先行":
            tier = "C"
        late = bool(held in ("耐えた", "同調") and reb == "遅れ")
        rows.append({"ticker": t, "own_dd_low_pct": fnum(own_low * 100), "r_held": fnum(r_held, 2), "held_class": held,
                     "own_dd_now_pct": fnum(own_now * 100), "r_now": fnum(r_now, 2), "rebound_class": reb,
                     "new_high_after_low": newhi, "below_50sma_days": below, "tier": tier, "late_break": late})
    return pd.DataFrame(rows).set_index("ticker")


def public_window(w):
    """signals.json 用（内部の位置情報を除く）。"""
    return {k: v for k, v in w.items() if not k.startswith("_")}


def isnull(v):
    """None / NaN / 空文字を欠損として扱う。"""
    if v is None:
        return True
    if isinstance(v, float) and math.isnan(v):
        return True
    return isinstance(v, str) and v.strip().lower() in ("", "nan", "none")


def jsonable(o):
    """NaN を None に、numpy 型を Python 型に直す（signals.json を厳密なJSONに保つ）。"""
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [jsonable(v) for v in o]
    if isinstance(o, (np.bool_,)):
        return bool(o)
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating, float)):
        return None if math.isnan(float(o)) or math.isinf(float(o)) else float(o)
    return o


# ----------------------------------------------------------------------------
# RSライン（E7のRSライン・RS新高値。v4.3b、P）
# ----------------------------------------------------------------------------
RSL = {"window": 252, "ema": 21, "lead_gap_pct": 2.0, "first_gap_days": 20}


def rs_lines(P, i, bench):
    """全銘柄のRSライン（P/B）の状態。RS新高値＝当日値が直近252本の過去値すべてを上回る（253本必要、E7）。
    先行＝RS新高値の日に、株価が252本終値高値から lead_gap_pct 以上下。EMAは21日（指数平滑、RSラインに適用）。"""
    C = P.C
    if bench not in C.columns:
        return None
    w = RSL["window"]
    R = C.div(C[bench], axis=0)
    prior = R.shift(1).rolling(w, min_periods=w).max()
    nh_all = R > prior
    E = R.ewm(span=RSL["ema"], adjust=False, min_periods=RSL["ema"]).mean()
    px_hi = C.rolling(w, min_periods=w).max()
    px_prior = C.shift(1).rolling(w, min_periods=w).max()
    r_i, p_i, e_i, e_y, r_y = R.iloc[i], prior.iloc[i], E.iloc[i], E.iloc[i - 1], R.iloc[i - 1]
    nh = (r_i > p_i).where(p_i.notna())
    px_gap = (C.iloc[i] / px_hi.iloc[i] - 1) * 100
    above, above_y = (r_i > e_i).where(e_i.notna()), (r_y > e_y).where(e_y.notna() & r_y.notna())
    past = nh_all.iloc[:i].to_numpy()
    since = {}
    for k, t in enumerate(R.columns):
        idx = np.flatnonzero(past[:, k])
        since[t] = int(i - idx[-1]) if len(idx) else None
    df = pd.DataFrame({
        "rs_new_high": nh,
        "leads_price": (nh == True) & (px_gap <= -RSL["lead_gap_pct"]),  # noqa: E712
        "price_new_high": (C.iloc[i] > px_prior.iloc[i]).where(px_prior.iloc[i].notna()),
        "days_since_prev_rs_high": pd.Series(since),
        "rs_pct_from_52w_high": (r_i / R.rolling(w, min_periods=w).max().iloc[i] - 1) * 100,
        "price_pct_from_52w_close_high": px_gap,
        "above_ema21": above,
        "cross_ema21": np.where(above.isna() | above_y.isna() | (above == above_y), None, np.where(above == True, "up", "down")),  # noqa: E712
        "dist_ema21_pct": (r_i / e_i - 1) * 100,
        "ema21_slope_5d_pct": (e_i / E.iloc[i - 5] - 1) * 100,
        "rs_up_vs_21d": (r_i > R.iloc[i - 21]).where(R.iloc[i - 21].notna()),  # E8 L の「RSライン > 21日前」
    })
    df["leads_price"] = df["leads_price"].astype(object)
    df.loc[df["rs_new_high"].isna(), "leads_price"] = None
    return df.drop(index=[bench], errors="ignore")


def rs_line_row(t, s, q, group=""):
    """signals.json 用の1行（s＝対SPY、q＝対QQQ の DataFrame）。"""
    r = s.loc[t]
    out = {"ticker": t, "group": group,
           "rs_new_high": None if isnull(r["rs_new_high"]) else bool(r["rs_new_high"]),
           "leads_price": None if isnull(r["leads_price"]) else bool(r["leads_price"]),
           "days_since_prev_rs_high": None if isnull(r["days_since_prev_rs_high"]) else int(r["days_since_prev_rs_high"]),
           "rs_pct_from_52w_high": fnum(r["rs_pct_from_52w_high"]), "price_pct_from_52w_close_high": fnum(r["price_pct_from_52w_close_high"]),
           "above_ema21": None if isnull(r["above_ema21"]) else bool(r["above_ema21"]), "cross_ema21": r["cross_ema21"],
           "dist_ema21_pct": fnum(r["dist_ema21_pct"], 2), "ema21_slope_5d_pct": fnum(r["ema21_slope_5d_pct"], 2),
           "rs_up_vs_21d": None if isnull(r["rs_up_vs_21d"]) else bool(r["rs_up_vs_21d"])}
    if q is not None and t in q.index:
        rq = q.loc[t]
        out["qqq"] = {"rs_new_high": None if isnull(rq["rs_new_high"]) else bool(rq["rs_new_high"]),
                      "above_ema21": None if isnull(rq["above_ema21"]) else bool(rq["above_ema21"]),
                      "rs_pct_from_52w_high": fnum(rq["rs_pct_from_52w_high"])}
    return out


# ----------------------------------------------------------------------------
# メイン
# ----------------------------------------------------------------------------
def pct(a, b):
    return round(100 * (a / b - 1), 2) if b and not math.isnan(a) and not math.isnan(b) else None


def replay_ledger(P, uni_stocks, i, n_days):
    """v4.6d（F2.3 補完）：空の台帳から i-n_days..i-1 の登録・ブレイク追跡・PP・利益確定の追跡をやり直す。
    当日（i）の処理は main の通常の流れで行う。毎日の実行で積み上がった台帳とは完全には一致しない
    （early_entry はレジーム依存で省略、ベースの番号は付けない、前回スクリーンとの差分イベントは出ない）。"""
    ledger = {"replayed": True, "replay_days": int(n_days)}
    start = max(260, i - int(n_days))
    stages = {}
    for j in range(start, i):
        day = P.dates[j]
        tstr = str(day.date())
        tt = P.tt7(j)
        if not stages or (j - start) % 5 == 0:
            spy_w, _ = weekly_bars(P, BENCH, j)
            stages = {t: weekly_stage(P, t, j, spy_w) for t in uni_stocks}
        setups = []
        for t in uni_stocks:
            if bool(tt.loc[t, "tt7_pass"]):
                vcp = detect_vcp(P, t, j)
                if vcp:
                    vcp["base_low"] = round(float(P.L[t].iloc[P.dates.get_loc(pd.Timestamp(vcp["base_start"])): j + 1].min()), 2)
                    setups.append({"ticker": t, **vcp})
                for det in (detect_cup, detect_flat_base, detect_double_bottom):
                    x = det(P, t, j)
                    if x:
                        setups.append({"ticker": t, **x})
            pp = detect_power_play(P, t, j)
            if pp:
                setups.append({"ticker": t, **pp})
        for s in setups:
            s["setup_type"] = "standard" if s["pattern"] in STANDARD_PATTERNS else ("power_play" if s["pattern"] == "power_play" else "early_entry")
            s["base_count"], s["base_count_status"] = None, "補完（未判定）"
            s["pattern_ja"] = PATTERN_JA.get(s["pattern"], s["pattern"])
        pps = []
        for t in uni_stocks:
            x = detect_pocket_pivot(P, t, j, tt.loc[t].to_dict(), stages[t])
            if x and x["qualified"]:
                pps.append(x)
        update_ledger(ledger, P, setups, day, j)
        register_pocket_pivots(ledger, pps, tstr, P, j)
        profit_tracking(ledger, P, j)
    return ledger


# ---------- 追補08 B4.5a：不安の指数（VIX）の期間構造。観察専用（Tape・Broad・L/H・買い判定には使わない） ----------
VOL_TERM = {"inverted_ge": 1.0, "calm_lt": 0.9, "recent_bars": 10, "window": 20}
VOL_TERM_JA = {"inverted": "逆転（目先の不安が3か月先より大きい。あわてた保険買いが入っている形）",
               "tight": "差が小さい（警戒が残っている）", "normal": "平常（目先より先のほうが高い）"}


def vol_term(P, i):
    """VIX（30日）÷VIX3M（3か月）ほか。tickers.txt に ^VIX3M・^VIX9D・^VVIX・^SKEW が無い・未取得なら該当項目は null。"""
    C = P.C

    def val(t, k):
        if t not in C.columns or k < 0:
            return None
        v = C[t].iloc[k]
        return None if pd.isna(v) else round(float(v), 2)

    names = ["^VIX", "^VIX9D", "^VIX3M", "^VVIX", "^SKEW"]
    out = {"definition_version": "VT-1.0", "as_of": str(P.dates[i].date()),
           "vix": val("^VIX", i), "vix9d": val("^VIX9D", i), "vix3m": val("^VIX3M", i), "vvix": val("^VVIX", i), "skew": val("^SKEW", i),
           "missing": [t for t in names if val(t, i) is None],
           "vix_over_vix3m": None, "vix_over_vix3m_5d_ago": None, "state": None, "state_ja": None, "days_in_state": None,
           "inverted_days_in_window": None, "last_inverted_date": None, "bars_since_inverted": None, "peak_ratio_in_window": None, "peak_ratio_date": None,
           "vix9d_over_vix": None, "unwinding": None,
           "definition": "vix_over_vix3m＝VIX（今後30日の不安）÷VIX3M（今後3か月）。1以上＝逆転、0.9未満＝平常。"
                         "unwinding＝直近10営業日に逆転があり、いまは逆転が解けて比率が5営業日前より低い（急いで買った保険を外す動きが進んだ形）。"
                         "VVIX＝VIXそのものの荒れ具合、SKEW＝大きな下落に備える保険の割高さ。観察専用"}
    if out["vix"] is not None and out["vix9d"] is not None and out["vix"] > 0:
        out["vix9d_over_vix"] = round(out["vix9d"] / out["vix"], 3)
    if "^VIX" not in C.columns or "^VIX3M" not in C.columns:
        return out
    r = (C["^VIX"] / C["^VIX3M"]).iloc[:i + 1].replace([np.inf, -np.inf], np.nan).dropna()
    if r.empty or r.index[-1] != P.dates[i]:
        return out

    def state_of(x):
        return "inverted" if x >= VOL_TERM["inverted_ge"] else ("normal" if x < VOL_TERM["calm_lt"] else "tight")

    st = r.apply(state_of)
    days = 0
    for v in st.iloc[::-1]:
        if v != st.iloc[-1]:
            break
        days += 1
    w = r.iloc[-VOL_TERM["window"]:]
    inv = st[st == "inverted"]
    last_inv = inv.index[-1] if not inv.empty else None
    since = int(len(r) - 1 - r.index.get_loc(last_inv)) if last_inv is not None else None
    r5 = float(r.iloc[-6]) if len(r) > 5 else None
    out.update({"vix_over_vix3m": round(float(r.iloc[-1]), 3), "vix_over_vix3m_5d_ago": round(r5, 3) if r5 is not None else None,
                "state": st.iloc[-1], "state_ja": VOL_TERM_JA[st.iloc[-1]], "days_in_state": days,
                "inverted_days_in_window": int((st.iloc[-VOL_TERM["window"]:] == "inverted").sum()),
                "last_inverted_date": str(last_inv.date()) if last_inv is not None else None, "bars_since_inverted": since,
                "peak_ratio_in_window": round(float(w.max()), 3), "peak_ratio_date": str(w.idxmax().date()),
                "unwinding": bool(st.iloc[-1] != "inverted" and since is not None and 0 < since <= VOL_TERM["recent_bars"] and r5 is not None and float(r.iloc[-1]) < r5)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--tickers", default="tickers.txt")
    ap.add_argument("--prev-ledger", default=None)
    ap.add_argument("--prev-screen", default=None, help="前回の screen.csv（v4.3 の差分イベント用。無ければ差分は出さない）")
    ap.add_argument("--prev-history", default=None, help="前回の history_signals.csv（data ブランチ）。無ければ --history-seed から始める")
    ap.add_argument("--history-seed", default="history_seed.csv", help="初回用の履歴（main の backfill）")
    ap.add_argument("--as-of", default=None, help="（v4.6d 補完用）この日付（YYYY-MM-DD）までの足だけで計算する。配信できなかった日のレポートを後日作るとき。出力先は data ブランチ以外にする")
    ap.add_argument("--replay-ledger", type=int, default=0, help="（v4.6d 補完用）台帳を空から n 営業日ぶん再生してから当日を処理する（--as-of と併用。前日時点の台帳は残っていないため）")
    a = ap.parse_args()
    out = a.out
    manifest = json.load(open(os.path.join(out, "manifest.json"), encoding="utf-8")) if os.path.exists(os.path.join(out, "manifest.json")) else {}

    uni = load_prices(os.path.join(out, "prices_daily.csv.gz"))
    sp = load_prices(os.path.join(out, "prices_sp500.csv.gz"))
    groups = parse_universe_groups(a.tickers)
    sectors = load_sp500_sectors(out)
    frames = [uni]
    if sp is not None:
        frames.append(sp[~sp["ticker"].isin(set(uni["ticker"]))])
    px = pd.concat(frames, ignore_index=True)
    cal = set(uni.loc[uni["ticker"] == BENCH, "date"])
    px = px[px["date"].isin(cal)]
    if a.as_of:                                                     # v4.6d 補完：as_of より後の足を捨てる
        asof_ts = pd.Timestamp(a.as_of)
        px = px[px["date"] <= asof_ts]
        if px.empty or px["date"].max() != asof_ts:
            print(json.dumps({"error": "as_of_not_trading_day", "as_of": a.as_of, "last_available": str(px["date"].max().date()) if not px.empty else None}, ensure_ascii=False))
            sys.exit(2)
    P = Panel(px)
    i = len(P.dates) - 1
    today = P.dates[i]
    tstr = str(today.date())

    uni_stocks = [t for t in uni["ticker"].unique() if is_stock_group(groups.get(t, "")) and t in P.C.columns]
    all_stocks = sorted(set(uni_stocks) | {t for t in P.C.columns if t in sectors})
    sector_of = lambda t: sectors.get(t) or ("Information Technology" if is_stock_group(groups.get(t, "")) else "other")

    # --- スクリーン（TT / RS） ---
    tt_now, rs_now = P.tt7(i), P.rs(i)
    tt_4w, rs_4w = P.tt7(i - 20), P.rs(i - 20)

    def rs_rank(rs_df):
        m = rs_df.loc[[t for t in all_stocks if t in rs_df.index], "rs_m"].dropna()
        return 100 * (m.rank(method="average") - 1) / (len(m) - 1) if len(m) > 1 else m * 0

    rank_now, rank_4w = rs_rank(rs_now), rs_rank(rs_4w)

    def leaders(tt_df, rank, rs_df):
        return [t for t in rank.index if bool(tt_df.loc[t, "tt7_pass"]) and rank[t] >= 80 and rs_df.loc[t, "rel_63"] > 0]

    leaders_now, leaders_4w = leaders(tt_now, rank_now, rs_now), leaders(tt_4w, rank_4w, rs_4w)
    sec_counts = {}
    for t in leaders_now:
        sec_counts[sector_of(t)] = sec_counts.get(sector_of(t), 0) + 1
    screen = pd.DataFrame({
        "ticker": all_stocks,
        "sector": [sector_of(t) for t in all_stocks],
        "in_universe": [t in uni_stocks for t in all_stocks],
        "group": [groups.get(t, "") for t in all_stocks],
        "close": [round(float(P.C[t].iloc[i]), 2) for t in all_stocks],
        "tt_count": [tt_now.loc[t, "tt_count"] for t in all_stocks],
        "tt7_pass": [bool(tt_now.loc[t, "tt7_pass"]) for t in all_stocks],
        "tt3_5m": [tt_now.loc[t, "tt3_5m"] for t in all_stocks],
        "rs_rank": [round(float(rank_now.get(t, np.nan)), 1) for t in all_stocks],
        "rel_21_pct": [round(100 * float(rs_now.loc[t, "rel_21"]), 1) for t in all_stocks],
        "rel_63_pct": [round(100 * float(rs_now.loc[t, "rel_63"]), 1) for t in all_stocks],
        "pct_from_52w_high": [round(100 * float(P.C[t].iloc[i] / P.h252[t].iloc[i] - 1), 1) for t in all_stocks],
        "pct_vs_50sma": [round(100 * float(P.C[t].iloc[i] / P.s50[t].iloc[i] - 1), 1) for t in all_stocks],
        "rvol50": [round(float(P.rvol50[t].iloc[i]), 2) if not math.isnan(P.rvol50[t].iloc[i]) else None for t in all_stocks],
        "leader": [t in leaders_now for t in all_stocks],
    })

    # --- E7.1 セクター相対強度 / E7.2 調整耐性（v4.3） ---
    def bench_pair(t):
        g = groups.get(t, "")
        if t in uni_stocks:
            for k, pair in GROUP_BENCH.items():
                if k in g:
                    return pair
            return ("XLK", "VGT")
        b = GICS_BENCH.get(sectors.get(t))
        return (b, None) if b else (None, None)

    bench_of = lambda t: bench_pair(t)[0]
    srs = sector_rs(P, all_stocks, bench_of, i)
    benches = sorted({b for b in (bench_of(t) for t in all_stocks) if b} | {BENCH})
    windows = {b: correction_window(P, b, i) for b in benches}
    res_parts = []
    for b in benches:
        if b == BENCH:
            continue
        members = [t for t in all_stocks if bench_of(t) == b]
        if members:
            res_parts.append(resilience(P, members, windows[b], i))
    def clean(df):
        """reindex で入る NaN を None に統一する（object 列）。"""
        return df.astype(object).replace({np.nan: None})

    res = pd.concat(res_parts) if res_parts else resilience(P, all_stocks, {"valid": False}, i)
    res = clean(res.reindex(all_stocks))
    res_spy = clean(resilience(P, uni_stocks, windows[BENCH], i).reindex(all_stocks))
    srs = clean(srs)
    held_ratio = {}
    for b in benches:
        members = [t for t in all_stocks if bench_of(t) == b] if b != BENCH else uni_stocks
        src = res if b != BENCH else res_spy
        hc = src.loc[[t for t in members if t in src.index], "held_class"].dropna()
        held_ratio[b] = {"held": int((hc == "耐えた").sum()), "computed": int(len(hc)),
                         "ratio_pct": fnum(100 * (hc == "耐えた").mean()) if len(hc) else None}

    for col in ("sector_bench", "rel_sec_21_pct", "rel_sec_63_pct", "dd_vs_sector_pt", "sector_rs_line_63h", "sector_rs_class"):
        screen[col] = [srs.loc[t, col] if t in srs.index else None for t in all_stocks]
    screen["res_bench"] = screen["sector_bench"]
    screen["res_state"] = [windows[b]["state"] if b in windows else None for b in screen["sector_bench"]]
    screen["res_dd_low_pct"] = [windows[b]["dd_low_pct"] if b in windows else None for b in screen["sector_bench"]]
    screen["res_dd_now_pct"] = [windows[b]["dd_now_pct"] if b in windows else None for b in screen["sector_bench"]]
    for col in ("own_dd_low_pct", "r_held", "held_class", "own_dd_now_pct", "r_now", "rebound_class",
                "new_high_after_low", "below_50sma_days"):
        screen[col] = res[col].values
    screen["next_candidate"] = res["tier"].values
    screen["late_break"] = res["late_break"].values
    screen["r_held_spy"] = res_spy["r_held"].values
    screen["r_now_spy"] = res_spy["r_now"].values

    # --- v4.3b: RSライン（対SPY、補助で対QQQ）。E7のRS新高値・21日EMA ---
    rsl_spy, rsl_qqq = rs_lines(P, i, BENCH), rs_lines(P, i, "QQQ")
    if rsl_spy is not None:
        rr = rsl_spy.reindex(all_stocks)
        screen["rs_line_new_high"] = rr["rs_new_high"].values
        screen["rs_line_leads_price"] = rr["leads_price"].values
        screen["rs_line_above_ema21"] = rr["above_ema21"].values
        screen["rs_line_pct_from_52w_high"] = [fnum(v) for v in rr["rs_pct_from_52w_high"]]

    # 前回の screen.csv との差分（key_events 用）。監視母集団だけ。
    rs_events = []
    if a.prev_screen and os.path.exists(a.prev_screen):
        try:
            prev = pd.read_csv(a.prev_screen).set_index("ticker")
        except Exception:
            prev = None
        if prev is not None and "sector_rs_class" in prev.columns:
            cur = screen.set_index("ticker")
            for t in uni_stocks:
                if t not in prev.index:
                    continue
                pc, nc = prev.loc[t, "sector_rs_class"], cur.loc[t, "sector_rs_class"]
                if not isnull(pc) and not isnull(nc) and {pc, nc} == {"leader", "laggard"}:
                    rs_events.append({"type": "sector_rs_flip", "ticker": t, "from": pc, "to": nc, "bench": cur.loc[t, "sector_bench"]})
                pt = prev.loc[t, "next_candidate"] if "next_candidate" in prev.columns else None
                nt = cur.loc[t, "next_candidate"]
                if not isnull(nt) and isnull(pt):
                    rs_events.append({"type": "next_candidate_new", "ticker": t, "tier": nt, "bench": cur.loc[t, "res_bench"],
                                      "held_class": cur.loc[t, "held_class"], "rebound_class": cur.loc[t, "rebound_class"]})
                if isnull(nt) and not isnull(pt):
                    rs_events.append({"type": "next_candidate_out", "ticker": t, "prev_tier": pt, "bench": cur.loc[t, "res_bench"],
                                      "held_class": cur.loc[t, "held_class"], "rebound_class": cur.loc[t, "rebound_class"], "r_now": cur.loc[t, "r_now"]})
                pl = "late_break" in prev.columns and str(prev.loc[t, "late_break"]).strip().lower() in ("true", "1")
                if str(cur.loc[t, "late_break"]).strip().lower() in ("true", "1") and not pl:
                    rs_events.append({"type": "late_break_new", "ticker": t, "bench": cur.loc[t, "res_bench"],
                                      "r_held": cur.loc[t, "r_held"], "r_now": cur.loc[t, "r_now"]})

    # --- v4.6b: 週足ステージ（E6.2。S&P500∪監視母集団と主要ETF）、業種別の分布（E6.3）、ベースカウント（E9.5a。監視母集団のみ） ---
    spy_w, _ = weekly_bars(P, BENCH, i)
    stage_etfs = list(dict.fromkeys(t for t in ["SPY", "QQQ", "IWM", "RSP", "DIA"] + TECH_ETFS + SECTOR_ETFS if t in P.C.columns))
    stages = {t: weekly_stage(P, t, i, spy_w) for t in sorted(set(all_stocks) | set(stage_etfs))}
    walks = {t: (base_walk(P, t, i, stages[t]["stage2_start"]) if stages[t].get("stage") == 2 and not stages[t].get("truncated") else None)
             for t in uni_stocks}
    screen["stage"] = [stages[t]["stage"] for t in all_stocks]
    screen["stage_label"] = [stages[t]["label"] for t in all_stocks]
    screen["stage_since"] = [stages[t]["since"] for t in all_stocks]
    screen["stage_truncated"] = [stages[t]["truncated"] for t in all_stocks]
    screen["stage_4w_ago"] = [stages[t]["sub_4w_ago"] for t in all_stocks]
    screen["mansfield_rs"] = [stages[t]["mansfield_rs"] for t in all_stocks]
    screen["base_count_current"] = [((walks.get(t) or {}).get("current") or {}).get("n") for t in all_stocks]
    screen["bases_completed"] = [(walks.get(t) or {}).get("count_completed") for t in all_stocks]
    grp_members = {}
    for gname in GROUP_BENCH:
        grp_members[f"監視：{gname}"] = [t for t in uni_stocks if gname in groups.get(t, "")]
    sp_members = [t for t in all_stocks if t in sectors]
    grp_members["S&P500全体"] = sp_members
    for sec in sorted({sectors[t] for t in sp_members}):
        grp_members[sec] = [t for t in sp_members if sectors[t] == sec]
    s_breadth = stage_breadth(stages, grp_members)

    screen.sort_values(["leader", "rs_rank"], ascending=[False, False]).to_csv(os.path.join(out, "screen.csv"), index=False)

    # --- 市場（C7.1） ---
    idx_state = {s: distribution_state(P.C[s], P.V[s], P.H[s], P.L[s], i) for s in ("^GSPC", "^IXIC") if s in P.C.columns}
    bw = load_breadth(out)
    if bw is not None and a.as_of:
        bw = bw[bw.index <= today]                                  # v4.6d 補完：市場内部も as_of まで
    fb = breakout_followthrough(P, all_stocks, i)
    fb_4w = breakout_followthrough(P, all_stocks, i - 20)
    fb["d5_hold_rate_4w_ago"] = fb_4w.get("d5_hold_rate")
    fq, fq_4w = fb_quality(fb), fb_quality(fb_4w)
    fb["extends"], fb["stalling"] = fq["extends"], fq["stalling"]                    # v4.4 (1)
    fb["d5_avg_ret_pct_4w_ago"], fb["stalling_4w_ago"] = fb_4w.get("d5_avg_ret_pct"), fq_4w["stalling"]
    bx, bx_y = breadth_ext(P, sectors, i), breadth_ext(P, sectors, i - 1)          # v4.4 (2)

    # --- チェックリスト（今日と昨日） ---
    chk_today = checklist(P, bw, leaders_now, leaders_4w, sec_counts, fb, idx_state, i, bx)
    idx_y = {s: distribution_state(P.C[s], P.V[s], P.H[s], P.L[s], i - 1) for s in idx_state}
    l_y, l_y4 = leaders(P.tt7(i - 1), rs_rank(P.rs(i - 1)), P.rs(i - 1)), leaders(P.tt7(i - 21), rs_rank(P.rs(i - 21)), P.rs(i - 21))
    chk_y = checklist(P, bw, l_y, l_y4, {}, breakout_followthrough(P, all_stocks, i - 1), idx_y, i - 1, bx_y)

    ledger = {}
    if a.replay_ledger > 0:                                         # v4.6d 補完：前日時点の台帳を再生で近似する
        ledger = replay_ledger(P, uni_stocks, i, a.replay_ledger)
        if a.prev_history and os.path.exists(a.prev_history):
            hh = pd.read_csv(a.prev_history)
            hh = hh[hh["as_of"].astype(str) < tstr].tail(130)
            ledger["regime_history"] = [{"date": str(r.as_of), "checklist_regime": r.regime_checklist, "confirmed_regime": r.regime_effective,
                                         "hysteresis": r.hysteresis, "provisional": bool(r.provisional)} for r in hh.itertuples()]
    elif a.prev_ledger and os.path.exists(a.prev_ledger):
        try:
            ledger = json.load(open(a.prev_ledger, encoding="utf-8"))
        except Exception:
            ledger = {}
    # 同じ基準日の再実行（GitHubの予約が1日に複数回動く場合）では、前日以前の確定値と比べる
    hist_before = [h for h in ledger.get("regime_history", []) if h.get("date", "") < tstr]
    prev_conf = hist_before[-1].get("confirmed_regime") if hist_before else None
    eff_regime, hyst = apply_hysteresis(chk_today, chk_y, prev_conf)

    # --- v4.5: 先導セクターの推移（A）、ヘッジ条件（B）、長期SPY配分モデル（C）。いずれも観察専用でレジームには使わない ---
    lead_rot, lead_events = leadership_rotation(P, uni_stocks, groups, sector_of, leaders_now, leaders_4w, l_y, l_y4, rank_now, rs_now, rs_4w, i)
    hedge = hedge_rules(P, chk_today, eff_regime, idx_state, i)
    hedge_y = hedge_rules(P, chk_y, prev_conf or chk_y["regime"], idx_y, i - 1)
    spym = spy_model(P, sectors, i)

    # --- セットアップ（テック・半導体のみ） ---
    setups = []
    for t in uni_stocks:
        tt_ok = bool(tt_now.loc[t, "tt7_pass"])
        vcp = detect_vcp(P, t, i) if tt_ok else None
        if vcp:
            vcp["base_low"] = round(float(P.L[t].iloc[P.dates.get_loc(pd.Timestamp(vcp["base_start"])): i + 1].min()), 2)
            setups.append({"ticker": t, **vcp})
            ee = detect_early_entry(P, t, i, vcp) if eff_regime in ("A", "B", "E") else None
            if ee:
                setups.append({"ticker": t, **ee})
        if tt_ok:                                                        # v4.6：カップ・フラットベース・ダブルボトム（E9.2a・E9.6・E9.7）
            for det in (detect_cup, detect_flat_base, detect_double_bottom):
                x = det(P, t, i)
                if x:
                    setups.append({"ticker": t, **x})
        pp = detect_power_play(P, t, i)
        if pp:
            setups.append({"ticker": t, **pp})
    for s in setups:
        t = s["ticker"]
        s["rs_rank"] = round(float(rank_now.get(t, np.nan)), 1)
        s["leader"] = t in leaders_now
        s["group"] = groups.get(t, "")
        s["sector_rs_class"] = srs.loc[t, "sector_rs_class"] if t in srs.index else None  # v4.3
        s["resilience_tier"] = res.loc[t, "tier"] if t in res.index else None          # v4.3
        # v4.6：Stage、ベースカウント、setup_type（E9.5・E12・E14.1）
        s["stage"], s["stage_label"] = stages[t].get("stage"), stages[t].get("label")
        if s["pattern"] in STANDARD_PATTERNS:
            n_, st_ = base_number(stages[t], walks[t], s["base_start"], s["base_low"])
            s["base_count"], s["base_count_status"] = n_, st_
            s["setup_type"] = "late_stage" if (n_ or 0) >= 4 else "standard"
            s["base_count_warning"] = bool(n_ == 3)
        else:
            s["base_count"], s["base_count_status"] = None, "対象外"
            s["setup_type"] = "power_play" if s["pattern"] == "power_play" else "early_entry"
        s["pattern_ja"] = PATTERN_JA.get(s["pattern"], s["pattern"])
    # 同じ銘柄に複数の標準パターンが出たら、ほかの型名を添える（件数は銘柄単位で数える）
    for s in setups:
        others = [x["pattern"] for x in setups if x["ticker"] == s["ticker"] and x is not s and x["pattern"] in STANDARD_PATTERNS]
        s["also"] = others if s["pattern"] in STANDARD_PATTERNS and others else []
    pd.DataFrame(setups).to_csv(os.path.join(out, "setups.csv"), index=False)

    # --- v4.6: ポケットピボット（E10.1a。テック・半導体のみ） ---
    pps = []
    for t in uni_stocks:
        x = detect_pocket_pivot(P, t, i, tt_now.loc[t].to_dict(), stages[t])
        if x:
            cur = (walks[t] or {}).get("current")
            x["base_count"] = cur["n"] if (x["kind"] == "in_base" and cur) else None
            x["stage"], x["stage_label"] = stages[t].get("stage"), stages[t].get("label")
            x["group"] = groups.get(t, "")
            x["rs_rank"] = round(float(rank_now.get(t, np.nan)), 1)
            x["leader"] = t in leaders_now
            pps.append(x)
    pps_ok = [x for x in pps if x["qualified"]]

    # --- 台帳更新 ---
    events = update_ledger(ledger, P, setups, today, i)
    events += register_pocket_pivots(ledger, pps_ok, tstr, P, i)   # v4.6
    pt_events, pt_rows = profit_tracking(ledger, P, i)               # v4.6（E13.2）
    events += pt_events
    events = events + jsonable(rs_events)  # v4.3 の差分イベントも台帳の day_events に残し、同日の再実行で消えないようにする
    # 同じ基準日に先の実行で検出したイベント（ブレイク等）を引き継ぐ。再実行では同じ事象が再検出されないため
    day = ledger.get("day_events") or {}
    if day.get("date") == tstr:
        seen = {json.dumps(e, sort_keys=True, ensure_ascii=False) for e in events}
        events = [e for e in day.get("events", []) if json.dumps(e, sort_keys=True, ensure_ascii=False) not in seen] + events
    ledger["day_events"] = {"date": tstr, "events": events}
    ledger["regime_history"] = [h for h in ledger.get("regime_history", []) if h.get("date") != tstr]
    ledger["regime_history"].append({"date": tstr, "checklist_regime": chk_today["regime"], "confirmed_regime": eff_regime,
                                     "hysteresis": hyst, "provisional": chk_today["provisional"]})
    ledger["regime_history"] = ledger["regime_history"][-130:]
    ledger["updated"] = tstr
    ledger["definition_version"] = DEF_VERSION
    json.dump(ledger, open(os.path.join(out, "ledger.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # --- 売りシグナル（先導候補＋母集団） ---
    sells = {}
    for t in sorted(set(uni_stocks) | set(leaders_now)):
        s = sell_signals(P, t, i)
        if s and (s["level"] or s["weakness"]):
            sells[t] = s

    # --- 決算（5営業日以内） ---
    earnings_soon = []
    ep = os.path.join(out, "events.csv")
    if os.path.exists(ep):
        ev = pd.read_csv(ep)
        ev["date_et"] = pd.to_datetime(ev["date_et"], errors="coerce")
        lim = today + pd.tseries.offsets.BDay(5)
        soon = ev[(ev["date_et"] >= today) & (ev["date_et"] <= lim)]
        earnings_soon = [{"ticker": r.ticker, "date_et": str(r.date_et.date()), "event": r.event} for r in soon.itertuples()]

    # --- 当日の重要イベント（日次で逃さないもの） ---
    key_events = list(events)
    for t in uni_stocks:
        if t in leaders_now and sells.get(t, {}).get("weakness"):
            key_events.append({"type": "leader_weakness", "ticker": t, "items": sells[t]["weakness"]})
        if sells.get(t, {}).get("level"):
            key_events.append({"type": sells[t]["level"], "ticker": t, "items": sells[t]["climax"]})
    for s in setups:
        if s["status"] in ("ready", "watch") and -3 <= s["dist_to_pivot_pct"] <= 0:
            key_events.append({"type": "near_pivot", "ticker": s["ticker"], "pattern": s["pattern"], "pivot": s["pivot"], "dist_pct": s["dist_to_pivot_pct"]})
    for e in earnings_soon:
        key_events.append({"type": "earnings_within_5d", **e})
    # v4.6b: ステージの変化（監視母集団と主要ETF）と業種の調子の変化。週の最後の取引日の足が入った日の実行だけで出す
    # （通常は金曜。金曜が休場の週は、その週が完了する次の営業日に出す＝直近の完了週の最終日が前営業日で、それが金曜でないとき）
    wk_end = stages.get(BENCH, {}).get("week_end")
    prev_day = P.dates[i - 1] if i >= 1 else None
    week_closed_now = wk_end == tstr or (prev_day is not None and str(prev_day.date()) == wk_end and prev_day.weekday() != 4)
    if week_closed_now:
        for t in list(uni_stocks) + [x for x in ["SPY", "QQQ", "IWM", "RSP", "DIA"] + TECH_ETFS if x in stages]:
            st = stages[t]
            prev, sub = st.get("stage_1w_ago"), st.get("sub")
            if not prev or not sub or prev == sub:
                continue
            if prev[0] != sub[0] or (sub == "1B" and prev != "1B"):
                key_events.append({"type": "stage_change", "ticker": t, "group": groups.get(t, "ETF"), "from": prev, "to": st["label"],
                                   "to_ja": st["label_ja"], "why": (st.get("last_change") or {}).get("why") if prev[0] != sub[0] else "／".join(st.get("reasons") or []),
                                   "pct_vs_ma30": st.get("pct_vs_ma30"), "mansfield_rs": st.get("mansfield_rs"),
                                   "support": st.get("support"), "resistance": st.get("resistance")})
        for g, b in s_breadth.items():
            if b.get("label_1w_ago") and b["label"] != b["label_1w_ago"]:
                key_events.append({"type": "sector_stage_shift", "group": g, "from": b["label_1w_ago"], "to": b["label"], "to_ja": b["label_ja"],
                                   "pct_stage": b["pct_stage"], "pct_stage_4w_ago": b["pct_stage_4w_ago"]})
    if eff_regime != prev_conf:
        key_events.append({"type": "regime_change", "from": prev_conf, "to": eff_regime, "hysteresis": hyst})
    # v4.4: 前営業日からの変化（同じ基準日の再実行でも同じ結果になるよう、i と i−1 の比較だけで決める）
    for gname in ("sp500", "tech"):
        a_, b_ = bx_y["groups"][gname], bx["groups"][gname]
        if a_.get("label") != b_.get("label") and not (a_.get("pct_down20") is None or b_.get("pct_down20") is None):
            key_events.append({"type": "breadth_label_change", "group": gname, "from": a_.get("label"), "to": b_.get("label"),
                               "to_ja": BX_LABEL_JA.get(b_.get("label"), "ラベルなし"), "pct_down20": b_.get("pct_down20"),
                               "bench": b_.get("bench"), "bench_pct_from_52w_high": b_.get("bench_pct_from_52w_high")})
    if chk_today.get("IX3_index_breakdown_guard") != chk_y.get("IX3_index_breakdown_guard"):
        key_events.append({"type": "index_breakdown_guard", "state": "on" if chk_today["IX3_index_breakdown_guard"] else "off",
                           "distribution_days": chk_today["IX2"], "spy_above_50": chk_today["SPY"]["above_50"],
                           "qqq_above_50": chk_today["QQQ"]["above_50"], "megacap_led": chk_today.get("megacap_led")})
    if chk_today.get("FB1_stalling") != chk_y.get("FB1_stalling"):
        key_events.append({"type": "breakout_stalling", "state": "on" if chk_today["FB1_stalling"] else "off",
                           "d5_hold_rate": fb.get("d5_hold_rate"), "d5_avg_ret_pct": fb.get("d5_avg_ret_pct"),
                           "d10_avg_ret_pct": fb.get("d10_avg_ret_pct")})
    # v4.5: 先導セクターの入れ替わり、ヘッジ条件の成立・解除、長期SPYモデルのシグナル変化
    key_events.extend(lead_events)
    for s in hedge["setups"]:
        key_events.append({"type": "hedge_setup", **s, "regime": eff_regime, "cover_conditions": "FTD成立またはレジームがA・B・E"})
    if hedge["cover_signal"] and not hedge_y["cover_signal"]:
        key_events.append({"type": "hedge_cover", "reasons": hedge["cover_reasons"]})
    if spym.get("status") == "ok" and spym["signal_since"] == tstr:
        key_events.append({"type": "spy_model_signal", "signal": spym["signal"], "composite": spym["composite"], "components": spym["components"]})

    # v4.3b: RSライン新高値（監視母集団の個別株と半導体・テクノロジーETF）。
    # 先行（株価は高値の下）は毎回、株価もそろって高値のときは前回のRS新高値から20営業日以上あいた場合だけ重要イベントにする。
    rs_line_out = {"status": "missing", "note": "SPYの日足がありません"}
    if rsl_spy is not None:
        rs_scope = [t for t in list(uni_stocks) + TECH_ETFS if t in rsl_spy.index]
        rows_rs = [rs_line_row(t, rsl_spy, rsl_qqq, groups.get(t, "")) for t in rs_scope]
        for r in rows_rs:
            if not r["rs_new_high"]:
                continue
            gap = r["days_since_prev_rs_high"]
            first = gap is None or gap >= RSL["first_gap_days"]
            if r["leads_price"] or first:
                key_events.append({"type": "rs_line_lead" if r["leads_price"] else "rs_line_new_high", "ticker": r["ticker"], "vs": BENCH,
                                   "price_pct_from_52w_close_high": r["price_pct_from_52w_close_high"], "days_since_prev_rs_high": gap})
        ok = [r for r in rows_rs if r["above_ema21"] is not None]
        mkt = rsl_spy.reindex([t for t in all_stocks if t in rsl_spy.index])
        spy_hi = P.C[BENCH].rolling(RSL["window"], min_periods=RSL["window"]).max().iloc[i]
        core = [t for t in ["SOXX", "SMH", "XLK", "IGV", "NVDA", "AVGO", "AMD", "TSM", "MU", "MSFT", "ORCL"] if t in rs_scope]
        rs_line_out = {
            "status": "ok", "bench": BENCH, "aux_bench": "QQQ",
            "definition": "RSライン＝終値÷SPY終値。RS新高値＝当日値が直近252本の過去値すべてを上回る（253本必要、E7）。"
                          f"先行＝RS新高値の日に株価が252本終値高値から{RSL['lead_gap_pct']}%以上下。EMA＝RSラインの21日指数平滑。"
                          "対QQQは補助表示。SOXLは対象外（SOXXで代用）。候補の絞り込みであり買い判定ではない。",
            "spy_pct_from_52w_close_high": fnum((P.C[BENCH].iloc[i] / spy_hi - 1) * 100),
            "market": {"population": int(mkt["rs_new_high"].notna().sum()),
                       "rs_new_high_count": int((mkt["rs_new_high"] == True).sum()),  # noqa: E712
                       "rs_leads_price_count": int((mkt["leads_price"] == True).sum()),  # noqa: E712
                       "above_ema21_pct": fnum(100 * (mkt["above_ema21"] == True).sum() / max(1, mkt["above_ema21"].notna().sum())),  # noqa: E712
                       "note": "S&P500∪監視母集団。市場全体の情報としてだけ使い、個別分析は監視母集団に限る"},
            "summary": {"rs_new_high": [r["ticker"] for r in rows_rs if r["rs_new_high"]],
                        "rs_leads_price": [r["ticker"] for r in rows_rs if r["leads_price"]],
                        "rs_near_high_5pct": [r["ticker"] for r in rows_rs if r["rs_pct_from_52w_high"] is not None and r["rs_pct_from_52w_high"] >= -5],
                        "above_ema21_count": f"{sum(r['above_ema21'] for r in ok)}/{len(ok)}",
                        "crossed_up": [r["ticker"] for r in rows_rs if r["cross_ema21"] == "up"],
                        "crossed_down": [r["ticker"] for r in rows_rs if r["cross_ema21"] == "down"],
                        "history_short": [r["ticker"] for r in rows_rs if r["rs_new_high"] is None]},
            "core": [r for r in rows_rs if r["ticker"] in core],
            "rows": sorted(rows_rs, key=lambda r: (not r["rs_new_high"], -(r["rs_pct_from_52w_high"] if r["rs_pct_from_52w_high"] is not None else -999))),
        }

    # --- 市場テーブル ---
    def row(t):
        c = P.C[t]
        return {"close": round(float(c.iloc[i]), 2), "chg_1d_pct": pct(c.iloc[i], c.iloc[i - 1]), "chg_1w_pct": pct(c.iloc[i], c.iloc[i - 5]),
                "chg_1m_pct": pct(c.iloc[i], c.iloc[i - 21]), "chg_3m_pct": pct(c.iloc[i], c.iloc[i - 63]),
                "vs_50sma_pct": pct(c.iloc[i], P.s50[t].iloc[i]), "vs_200sma_pct": pct(c.iloc[i], P.s200[t].iloc[i]),
                "rel_spy_1m_pct": round(100 * float(rs_now.loc[t, "rel_21"]), 2) if t in rs_now.index else None,
                "rel_spy_3m_pct": round(100 * float(rs_now.loc[t, "rel_63"]), 2) if t in rs_now.index else None,
                "pct_from_52w_high": round(100 * float(c.iloc[i] / P.h252[t].iloc[i] - 1), 1) if not math.isnan(P.h252[t].iloc[i]) else None,
                "rvol50": round(float(P.rvol50[t].iloc[i]), 2) if not math.isnan(P.rvol50[t].iloc[i]) else None}

    market = {t: row(t) for t in ["^GSPC", "^IXIC", "SPY", "QQQ", "IWM", "RSP", "DIA", "^VIX", "TLT", "HYG", "LQD", "UUP", "GLD", "USO"] if t in P.C.columns}
    sector_etf = {t: row(t) for t in SECTOR_ETFS if t in P.C.columns}
    tech_etf = {t: row(t) for t in TECH_ETFS + ["SOXL"] if t in P.C.columns}

    tech_leaders = [{"ticker": t, "group": groups.get(t, ""), "rs_rank": round(float(rank_now[t]), 1), "rel_63_pct": round(100 * float(rs_now.loc[t, "rel_63"]), 1),
                     "pct_from_52w_high": round(100 * float(P.C[t].iloc[i] / P.h252[t].iloc[i] - 1), 1), "tt3_5m": bool(tt_now.loc[t, "tt3_5m"]),
                     "sector_rs_class": srs.loc[t, "sector_rs_class"], "resilience_tier": res.loc[t, "tier"],
                     "stage": stages[t].get("stage"), "stage_label": stages[t].get("label"),
                     "base_count_current": ((walks.get(t) or {}).get("current") or {}).get("n")}   # v4.6
                    for t in leaders_now if t in uni_stocks]
    tech_watch = [{"ticker": t, "group": groups.get(t, ""), "tt_count": int(tt_now.loc[t, "tt_count"]) if not math.isnan(tt_now.loc[t, "tt_count"]) else None,
                   "rs_rank": round(float(rank_now.get(t, np.nan)), 1), "rel_63_pct": round(100 * float(rs_now.loc[t, "rel_63"]), 1),
                   "sector_rs_class": srs.loc[t, "sector_rs_class"], "resilience_tier": res.loc[t, "tier"]}
                  for t in uni_stocks if t not in leaders_now and (rank_now.get(t, 0) >= 80 or bool(tt_now.loc[t, "tt7_pass"]))]

    # --- v4.3: セクター相対強度・調整耐性の要約（監視母集団） ---
    def srow(t):
        r = srs.loc[t]
        return {"ticker": t, "rel_sec_21_pct": r["rel_sec_21_pct"], "rel_sec_63_pct": r["rel_sec_63_pct"],
                "dd_vs_sector_pt": r["dd_vs_sector_pt"], "rs_line_63h": r["sector_rs_line_63h"], "class": r["sector_rs_class"],
                "rs_rank": fnum(rank_now.get(t, np.nan)), "tt_count": fnum(tt_now.loc[t, "tt_count"], 0)}

    sector_rs_groups = {}
    for gname, (b, confirm) in GROUP_BENCH.items():
        members = [t for t in uni_stocks if gname in groups.get(t, "") and srs.loc[t, "sector_rs_class"] is not None]
        rows_ = sorted((srow(t) for t in members), key=lambda r: -(r["rel_sec_63_pct"] if r["rel_sec_63_pct"] is not None else -1e9))
        cnt = {k: sum(1 for r in rows_ if r["class"] == k) for k in ("leader", "neutral", "laggard")}
        bdd = fnum(100 * (P.C[b].iloc[i] / P.h252[b].iloc[i] - 1)) if b in P.C.columns else None
        sector_rs_groups[gname] = {"bench": b, "confirm": confirm, "bench_pct_from_52w_high": bdd,
                                   "bench_rel_spy_63_pct": fnum(100 * rs_now.loc[b, "rel_63"]) if b in rs_now.index else None,
                                   "counts": cnt,
                                   "leaders": [r["ticker"] for r in rows_ if r["class"] == "leader"][:10],
                                   "laggards": [r["ticker"] for r in reversed(rows_) if r["class"] == "laggard"][:10],
                                   "rows": rows_}

    def rrow(t):
        r = res.loc[t]
        return {"ticker": t, "group": groups.get(t, ""), "bench": bench_of(t), "tier": r["tier"],
                "held_class": r["held_class"], "r_held": r["r_held"], "own_dd_low_pct": r["own_dd_low_pct"],
                "rebound_class": r["rebound_class"], "r_now": r["r_now"], "own_dd_now_pct": r["own_dd_now_pct"],
                "new_high_after_low": r["new_high_after_low"], "below_50sma_days": r["below_50sma_days"],
                "tt_count": fnum(tt_now.loc[t, "tt_count"], 0), "rs_rank": fnum(rank_now.get(t, np.nan)),
                "rel_63_pct": fnum(100 * rs_now.loc[t, "rel_63"]), "sector_rs_class": srs.loc[t, "sector_rs_class"]}

    tier_order = {"A": 0, "B": 1, "C": 2}
    next_candidates = sorted((rrow(t) for t in uni_stocks if not isnull(res.loc[t, "tier"])),
                             key=lambda r: (tier_order[r["tier"]], r["r_held"] if r["r_held"] is not None else 9e9, -(r["rel_63_pct"] or 0)))
    late_breaks = sorted((rrow(t) for t in uni_stocks if res.loc[t, "late_break"] is True), key=lambda r: -(r["r_now"] or 0))
    sp500_next = [t for t in all_stocks if t not in uni_stocks and res.loc[t, "tier"] in ("A", "B")]
    correction_resilience = {
        "definition_version": "ST-1.3",
        "windows": [public_window(windows[b]) for b in benches],
        "next_candidates": next_candidates,
        "late_breaks": late_breaks,
        "held_ratio": held_ratio,
        "sp500_next_candidates_ab": {"count": len(sp500_next), "tickers": sorted(sp500_next, key=lambda t: res.loc[t, "r_held"] if res.loc[t, "r_held"] is not None else 9e9)[:30]},
        "note": "監視候補の順番であり買い判定ではない。R_held・R_nowは倍率なので dd_low・dd_now を併記して読む。",
    }

    # --- v4.6: setup_type別の件数（銘柄単位）、ステージの一覧 ---
    setup_counts = {}
    for s in setups:
        d = setup_counts.setdefault(s["setup_type"], {})
        d.setdefault(s["status"], set()).add(s["ticker"])
    setup_counts = {k: {st: {"count": len(v), "tickers": sorted(v)} for st, v in d.items()} for k, d in setup_counts.items()}
    setup_counts["pocket_pivot"] = {"today": {"count": len(pps_ok), "tickers": sorted({x["ticker"] for x in pps_ok})}}
    stage_rows = []
    for t in uni_stocks:
        st, wk = stages[t], walks.get(t) or {}
        cur = wk.get("current") or {}
        bstat = base_number(st, walks.get(t), "9999-12-31", float("inf"))[1]
        stage_rows.append({"ticker": t, "group": groups.get(t, ""),
                           **{k: st.get(k) for k in ("label", "label_ja", "stage", "sub", "qual", "since", "weeks_in_stage", "truncated",
                                                     "pct_vs_ma30", "ma30", "ma30_slope_4w_pct", "mansfield_rs", "mansfield_rs_4w_ago",
                                                     "support", "resistance", "breakout", "reasons", "last_change", "history",
                                                     "sub_4w_ago", "provisional", "note")},
                           "bases_completed": wk.get("count_completed"), "in_base": bool(cur), "current_base": cur.get("n"),
                           "current_base_start": cur.get("start"), "current_base_depth_pct": cur.get("depth_pct"),
                           "base_count_status": "ok" if wk else bstat, "last_bases": wk.get("completed", [])[-3:]})
    stage_counts = {x: sum(1 for r in stage_rows if r["sub"] == x) for x in SUBS}
    stage_counts["unclassified"] = sum(1 for r in stage_rows if r["sub"] is None)
    etf_rows = [{"ticker": t, **{k: stages[t].get(k) for k in ("label", "label_ja", "stage", "since", "weeks_in_stage", "pct_vs_ma30",
                                                             "ma30_slope_4w_pct", "mansfield_rs", "support", "resistance", "history", "sub_4w_ago", "provisional")}}
                for t in stage_etfs]

    signals = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_market_date": tstr,
        "definition_version": DEF_VERSION,
        # v4.6d：補完実行（--as-of）のとき。通常は null。レポートは「補完版」と明記し、前回スクリーン差分・台帳の完全一致は保証しない
        "backfill": ({"as_of_requested": a.as_of, "ledger_replayed_days": a.replay_ledger, "prev_screen_diff": bool(a.prev_screen),
                      "note": "後日の補完。台帳は空から再生（early_entry・前回スクリーン差分は含まない）"} if a.as_of else None),
        "data": {"universe_tickers": len(uni_stocks), "sp500_tickers": len([t for t in all_stocks if t in sectors]), "rs_rank_population": int(len(rank_now)),
                 "breadth_as_of": chk_today.get("breadth_as_of"), "manifest": {k: manifest.get(k) for k in ("generated_jst", "repo_last_commit_main", "keepalive")},
                 "prices": manifest.get("prices", {}), "note": "市場内部とブレイク追随率は現在のS&P500構成銘柄から計算したproxy（生存者バイアスあり）"},
        "tape": {"regime_checklist": chk_today["regime"], "regime_effective": eff_regime, "hysteresis": hyst, "provisional": chk_today["provisional"],
                 "reasons": chk_today["regime_reasons"], "previous_confirmed": prev_conf, "yesterday_checklist": chk_y["regime"],
                 "checklist": {k: v for k, v in chk_today.items() if k not in ("regime", "regime_reasons", "provisional", "unverified")},
                 "unverified": chk_today["unverified"]},
        "index_state": idx_state,
        "breakout_followthrough_market": fb,
        "breadth_ext": jsonable({**bx,
                                 "labels_1d_ago": {k: bx_y["groups"][k].get("label") for k in ("sp500", "tech")},
                                 "label_ja": BX_LABEL_JA,
                                 "definition": f"pct_down20＝終値が52週高値（日中高値）の{100 - BRX['down_pct']:.0f}%以下の銘柄の比率。"
                                               "グループはS&P500の現在の構成銘柄（proxy、生存者バイアスあり）。tech＝IT＋通信、ex_tech＝それ以外。"
                                               f"ラベル：過半（{BRX['stealth']:.0f}%以上）かつ基準ETFが52週高値から{BRX['near_high']:.0f}%以内→stealth、"
                                               f"基準ETFも{BRX['near_high']:.0f}%超→broad_decline、{BRX['watch']:.0f}%以上かつ高値圏→watch。"
                                               f"megacap_led＝QQQのSPY比1か月<={BRX['megacap_qqq_rel_1m']}%かつRSP/SPY 4週>=+{BRX['megacap_rsp_spy_4w']}%。"
                                               "Tapeレジームに使うのはS&P500のstealthだけ（Cの支持）。techは半導体・テクノロジー節の環境認識に使う。"}),
        "market": market, "sector_etf": sector_etf, "tech_etf": tech_etf,
        "vol_term": jsonable(vol_term(P, i)),   # 追補08 B4.5a（観察専用）
        "leaders_market": {"count": len(leaders_now), "count_4w_ago": len(leaders_4w), "by_sector": sec_counts, "tickers": leaders_now[:60]},
        "leadership": jsonable(lead_rot),   # v4.5 (A)
        "hedge": jsonable(hedge),           # v4.5 (B)
        "spy_model": jsonable(spym),        # v4.5 (C)
        "tech_leaders": sorted(tech_leaders, key=lambda x: -x["rs_rank"]),
        "tech_watch": sorted(tech_watch, key=lambda x: -(x["rs_rank"] or 0))[:25],
        "setups": sorted(setups, key=lambda s: ({"ready": 0, "watch": 1, "forming": 2}.get(s["status"], 3), -(s.get("rs_rank") or 0))),
        "setup_counts": setup_counts,                                    # v4.6：setup_type別・状態別（銘柄単位）
        "pocket_pivots": {"definition_version": "PP-1.0", "today": pps_ok, "excluded": [x for x in pps if not x["qualified"]],
                          "note": "E10.1a。当日出来高＞直前10営業日の下落日の最大出来高、上昇引け、10日線か50日線の近く（安値が線の±2%、終値が線の0〜5%上）、"
                                  "50日線以上、150・200日線の上で200日線が上向き。V字・ウェッジ・損切り幅7%超は除外。業績は未確認（unverified）。"
                                  "補助シグナルであり、VCP・通常ブレイクとは別系列で追跡する。"},
        "profit_tracking": {"definition_version": "PT-1.0", "rows": pt_rows,
                            "note": "E13.2。ブレイク（PPはシグナル日）から60営業日まで。基準＝ピボット（PPは当日終値）。"
                                    "+20〜25%は利益確定の検討帯、3週以内に+20%なら8週（40営業日）保有ルール。自動売却ではなく、保有を明示した銘柄だけ行動に結びつける。"},
        "stages": {"definition_version": "ST6-2.0", "week_end": stages.get(BENCH, {}).get("week_end"),
                   "counts": stage_counts, "rows": stage_rows, "etfs": etf_rows, "breadth": jsonable(s_breadth),
                   "label_ja": STAGE_JA, "breadth_label_ja": SB_LABEL_JA,
                   "note": "E6.2・E6.3（07追補）。週足（完了週）の30週SMAとマンスフィールドRS（対SPY、52週）で、ステージ1〜4と細区分1A〜4B-を状態遷移で決める。"
                           "＋＝市場比の強さがプラスで改善中、−＝マイナスで悪化中。provisional＝進行中の週の暫定。"
                           "breadth＝業種ごとの個別株の分布（S&P500は現在の構成銘柄。監視グループは監視母集団）。"},
        "ledger_cohorts": ledger_cohorts(ledger),
        "ledger_open": [s for s in ledger["setups"] if s.get("status") in ("registered", "breakout")],
        "sell_signals": sells,
        "earnings_within_5d": earnings_soon,
        "key_events_today": key_events,
        "sector_rs": jsonable({"definition_version": "ST-1.3", "groups": sector_rs_groups,
                               "note": "対セクターETFの補助表示。市場RS百分位（rs_rank）・先導候補の条件・Tapeスコアには使わない（E7.1）。"}),
        "correction_resilience": jsonable(correction_resilience),
        "rs_lines": jsonable(rs_line_out),
    }
    json.dump(signals, open(os.path.join(out, "signals.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    # v4.5b: 判定結果の日次履歴（閾値の検証用。同じ基準日の再実行は上書き）
    hrow = history_row(tstr, signals["generated_utc"], chk_today, eff_regime, hyst, idx_state, lead_rot, hedge, spym, P, i, setups, key_events,
                       sbreadth=s_breadth)
    n_hist, hist_src = write_history(out, a.prev_history, a.history_seed, hrow)
    print(json.dumps({"history_signals_rows": n_hist, "history_from": hist_src}, ensure_ascii=False))
    print(json.dumps({"as_of": tstr, "regime": eff_regime, "hysteresis": hyst, "leaders_market": len(leaders_now), "tech_leaders": len(tech_leaders),
                      "setups_ready": sum(s["status"] == "ready" for s in setups), "key_events": len(key_events)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
