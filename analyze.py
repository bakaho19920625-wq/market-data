"""
米国株指標チェック 解析エンジン（GitHub Actionsで fetch_data.py の後に実行）

入力（--out フォルダ内、fetch_data.py の出力）:
  prices_daily.csv.gz   監視母集団（テック・半導体＋指数・ETF）の日足 約3年
  prices_sp500.csv.gz   S&P500構成銘柄の日足 約2年（市場全体のRS・先導株・ブレイク統計用）
  breadth.csv           市場内部（proxy）
  events.csv            決算予定日（取得できた分）
  manifest.json
  prev/ledger.json      前回の台帳（workflowが前回のdataブランチから持ってくる。無ければ初回）

出力（--out フォルダ）:
  signals.json   日次・週次レポートが読む要約（市場、Tapeチェックリスト、先導株、セットアップ、当日の重要イベント）
  screen.csv     母集団＋S&P500の全銘柄スクリーン（TT、RS順位、位置）
  setups.csv     テック・半導体のセットアップ一覧
  ledger.json    台帳（セットアップ登録、ブレイク追跡、レジーム履歴）。次回に引き継ぐ

定義はマスター仕様 v4.2 の C7.1 / C15.1 / E4〜E14 に対応する。閾値はすべて P（プロジェクト規則）。
"""
import argparse
import json
import math
import os
import re
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

DEF_VERSION = "ST-1.2 / engine 1.0"
JST = timezone(timedelta(hours=9))
INDEX_TICKERS = {"^GSPC", "^IXIC", "^VIX"}
SECTOR_ETFS = ["XLK", "XLC", "XLY", "XLP", "XLE", "XLF", "XLV", "XLI", "XLB", "XLRE", "XLU"]
TECH_ETFS = ["SOXX", "SMH", "XLK", "VGT", "IGV", "CIBR", "SKYY"]
BENCH = "SPY"


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
        "pct_from_52w_high": float(c.iloc[-1] / hi252.iloc[-1] - 1) if not math.isnan(hi252.iloc[-1]) else None,
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
# Tapeチェックリスト（C15.1 改訂版）
# ----------------------------------------------------------------------------
def checklist(P, bw, leaders_now, leaders_4w, leader_sectors, fb, idx_state, i):
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
    unverified = []
    if br1 is None or br2 is None:
        unverified.append("breadth")
    # 判定（F → C → D → E → A → B）
    regime, reasons = None, []
    f_core = (spy["ma200_falling"] and not spy["above_200"]) or (qqq["ma200_falling"] and not qqq["above_200"])
    f_sup = sum([items["IX2"] >= 6, (br1 or 100) < 30, (br2 or 1) < 0, len(leaders_now) == 0 or len(leaders_now) < len(leaders_4w), (fb5 or 100) < 30])
    if f_core and f_sup >= 2:
        regime, reasons = "F", ["下降中の200日線下", f"支持{f_sup}件"]
    if regime is None:
        c_core = spy["pct_from_52w_high"] >= -10 or qqq["pct_from_52w_high"] >= -10
        c_sup = sum([items["IX2"] >= 5, br1 is not None and br1p is not None and br1 <= br1p - 10, br2 is not None and br2p is not None and br2 < 0 and br2 < br2p,
                     (fb5 or 100) < 40, (items["BR3_rsp_spy_4w_pct"] or 0) <= -2])
        if c_core and c_sup >= 2:
            regime, reasons = "C", ["高値圏", f"分配・内部悪化の支持{c_sup}件"]
    e_ftd = ftd_ok and spy["above_21ema"] and spy["above_50"] and qqq["above_21ema"] and qqq["above_50"]
    recov_items = [br1 is not None and br1p is not None and br1 >= br1p + 15,
                   br2 is not None and br2p is not None and br2 > 0 and br2 > br2p,
                   len(leaders_now) >= len(leaders_4w),
                   (items["BR3_rsp_spy_4w_pct"] or 0) > 0]
    e_recov = spy["above_200"] and qqq["above_200"] and spy["ma200_rising"] and qqq["ma200_rising"] and sum(recov_items) >= 2
    items["E_recovery_items_met"] = int(sum(recov_items))
    e_sup = sum([br1 is not None and br1p is not None and br1 >= br1p + 10, len(leaders_now) > len(leaders_4w), (items["BR3_rsp_spy_4w_pct"] or 0) > 0,
                 fb.get("d5_hold_rate") is not None and fb5 >= 50])
    if regime is None:
        d_core = spy["pct_from_52w_high"] < -10 or qqq["pct_from_52w_high"] < -10 or not spy["above_200"] or not qqq["above_200"]
        if d_core and not (e_ftd or e_recov):
            regime, reasons = "D", ["調整中または200日線下、回復未確認"]
    if regime is None and ((e_ftd and e_sup >= 1) or e_recov) and not (spy["IX1"] and qqq["IX1"]):
        regime, reasons = "E", ["FTD成立" if e_ftd else f"FTDなしの内部回復（回復項目{sum(recov_items)}/4）", f"支持{e_sup}件"]
    a_core = (spy["IX1"] and qqq["IX1"] and items["IX2"] <= 3 and br1 is not None and br1 >= 60 and br1 >= (br1p or 0) and (br2 or -1) > 0
              and len(leaders_now) >= len(leaders_4w) and len(leaders_now) >= 10 and len(leader_sectors) >= 3 and fb_ok and fb5 >= 50)
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
            elif l <= Sv:
                s["status"] = "failed"
                events.append({"type": "setup_failed", "ticker": t, "pattern": s["pattern"], "stop": Sv})
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
        for s in same:
            if s.get("status") == "registered":
                s["status"] = "superseded"  # 形が変わった。新しい登録に置き換える
        if key not in by_key:
            rec = {"setup_id": f"{u['ticker']}-{u['pattern']}-{tstr}", "ticker": u["ticker"], "pattern": u["pattern"], "registered": tstr,
                   "pivot": u["pivot"], "stop": u["stop"], "risk_pct": u["risk_pct"], "status": "registered", "breakout_date": None,
                   "definition_version": DEF_VERSION}
            setups.append(rec)
            by_key[key] = rec
            events.append({"type": "new_ready", "ticker": u["ticker"], "pattern": u["pattern"], "pivot": u["pivot"], "stop": u["stop"], "risk_pct": u["risk_pct"]})
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


def ledger_cohorts(ledger):
    """E14.3: prospectiveのD+3/5/10集計（setup_type別）。"""
    out = {}
    for s in ledger.get("setups", []):
        if s.get("breakout_date") is None or s.get("observation") != "prospective":
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
# メイン
# ----------------------------------------------------------------------------
def pct(a, b):
    return round(100 * (a / b - 1), 2) if b and not math.isnan(a) and not math.isnan(b) else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--tickers", default="tickers.txt")
    ap.add_argument("--prev-ledger", default=None)
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
    screen.sort_values(["leader", "rs_rank"], ascending=[False, False]).to_csv(os.path.join(out, "screen.csv"), index=False)

    # --- 市場（C7.1） ---
    idx_state = {s: distribution_state(P.C[s], P.V[s], P.H[s], P.L[s], i) for s in ("^GSPC", "^IXIC") if s in P.C.columns}
    bw = load_breadth(out)
    fb = breakout_followthrough(P, all_stocks, i)
    fb_4w = breakout_followthrough(P, all_stocks, i - 20)
    fb["d5_hold_rate_4w_ago"] = fb_4w.get("d5_hold_rate")

    # --- チェックリスト（今日と昨日） ---
    chk_today = checklist(P, bw, leaders_now, leaders_4w, sec_counts, fb, idx_state, i)
    idx_y = {s: distribution_state(P.C[s], P.V[s], P.H[s], P.L[s], i - 1) for s in idx_state}
    l_y, l_y4 = leaders(P.tt7(i - 1), rs_rank(P.rs(i - 1)), P.rs(i - 1)), leaders(P.tt7(i - 21), rs_rank(P.rs(i - 21)), P.rs(i - 21))
    chk_y = checklist(P, bw, l_y, l_y4, {}, breakout_followthrough(P, all_stocks, i - 1), idx_y, i - 1)

    ledger = {}
    if a.prev_ledger and os.path.exists(a.prev_ledger):
        try:
            ledger = json.load(open(a.prev_ledger, encoding="utf-8"))
        except Exception:
            ledger = {}
    # 同じ基準日の再実行（GitHubの予約が1日に複数回動く場合）では、前日以前の確定値と比べる
    hist_before = [h for h in ledger.get("regime_history", []) if h.get("date", "") < tstr]
    prev_conf = hist_before[-1].get("confirmed_regime") if hist_before else None
    eff_regime, hyst = apply_hysteresis(chk_today, chk_y, prev_conf)

    # --- セットアップ（テック・半導体のみ） ---
    setups = []
    for t in uni_stocks:
        tt_ok = bool(tt_now.loc[t, "tt7_pass"])
        vcp = detect_vcp(P, t, i) if tt_ok else None
        if vcp:
            setups.append({"ticker": t, **vcp})
            ee = detect_early_entry(P, t, i, vcp) if eff_regime in ("A", "B", "E") else None
            if ee:
                setups.append({"ticker": t, **ee})
        pp = detect_power_play(P, t, i)
        if pp:
            setups.append({"ticker": t, **pp})
    for s in setups:
        s["rs_rank"] = round(float(rank_now.get(s["ticker"], np.nan)), 1)
        s["leader"] = s["ticker"] in leaders_now
        s["group"] = groups.get(s["ticker"], "")
    pd.DataFrame(setups).to_csv(os.path.join(out, "setups.csv"), index=False)

    # --- 台帳更新 ---
    events = update_ledger(ledger, P, setups, today, i)
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
    if eff_regime != prev_conf:
        key_events.append({"type": "regime_change", "from": prev_conf, "to": eff_regime, "hysteresis": hyst})

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
                     "pct_from_52w_high": round(100 * float(P.C[t].iloc[i] / P.h252[t].iloc[i] - 1), 1), "tt3_5m": bool(tt_now.loc[t, "tt3_5m"])}
                    for t in leaders_now if t in uni_stocks]
    tech_watch = [{"ticker": t, "group": groups.get(t, ""), "tt_count": int(tt_now.loc[t, "tt_count"]) if not math.isnan(tt_now.loc[t, "tt_count"]) else None,
                   "rs_rank": round(float(rank_now.get(t, np.nan)), 1), "rel_63_pct": round(100 * float(rs_now.loc[t, "rel_63"]), 1)}
                  for t in uni_stocks if t not in leaders_now and (rank_now.get(t, 0) >= 80 or bool(tt_now.loc[t, "tt7_pass"]))]

    signals = {
        "generated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "as_of_market_date": tstr,
        "definition_version": DEF_VERSION,
        "data": {"universe_tickers": len(uni_stocks), "sp500_tickers": len([t for t in all_stocks if t in sectors]), "rs_rank_population": int(len(rank_now)),
                 "breadth_as_of": chk_today.get("breadth_as_of"), "manifest": {k: manifest.get(k) for k in ("generated_jst", "repo_last_commit_main", "keepalive")},
                 "prices": manifest.get("prices", {}), "note": "市場内部とブレイク追随率は現在のS&P500構成銘柄から計算したproxy（生存者バイアスあり）"},
        "tape": {"regime_checklist": chk_today["regime"], "regime_effective": eff_regime, "hysteresis": hyst, "provisional": chk_today["provisional"],
                 "reasons": chk_today["regime_reasons"], "previous_confirmed": prev_conf, "yesterday_checklist": chk_y["regime"],
                 "checklist": {k: v for k, v in chk_today.items() if k not in ("regime", "regime_reasons", "provisional", "unverified")},
                 "unverified": chk_today["unverified"]},
        "index_state": idx_state,
        "breakout_followthrough_market": fb,
        "market": market, "sector_etf": sector_etf, "tech_etf": tech_etf,
        "leaders_market": {"count": len(leaders_now), "count_4w_ago": len(leaders_4w), "by_sector": sec_counts, "tickers": leaders_now[:60]},
        "tech_leaders": sorted(tech_leaders, key=lambda x: -x["rs_rank"]),
        "tech_watch": sorted(tech_watch, key=lambda x: -(x["rs_rank"] or 0))[:25],
        "setups": sorted(setups, key=lambda s: ({"ready": 0, "watch": 1, "forming": 2}.get(s["status"], 3), -(s.get("rs_rank") or 0))),
        "ledger_cohorts": ledger_cohorts(ledger),
        "ledger_open": [s for s in ledger["setups"] if s.get("status") in ("registered", "breakout")],
        "sell_signals": sells,
        "earnings_within_5d": earnings_soon,
        "key_events_today": key_events,
    }
    json.dump(signals, open(os.path.join(out, "signals.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    print(json.dumps({"as_of": tstr, "regime": eff_regime, "hysteresis": hyst, "leaders_market": len(leaders_now), "tech_leaders": len(tech_leaders),
                      "setups_ready": sum(s["status"] == "ready" for s in setups), "key_events": len(key_events)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
