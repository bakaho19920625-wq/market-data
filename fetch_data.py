"""
米国株データ自動取得スクリプト（GitHub Actionsで毎日実行）

出力（--outで指定したフォルダ、既定は out/）:
  prices_daily.csv.gz : tickers.txt の銘柄の日足（約3年分）
                        価格は分割調整済み・配当非調整（Yahooの終値）。
                        配当込みの調整後終値は adj_close 列に別途保存。
  breadth.csv         : S&P500構成銘柄の株価から計算した市場内部（proxy）
                        50日線・200日線上比率、52週新高値・新安値、騰落数
  manifest.json       : 実行日時、基準日、件数、取得できなかった銘柄など

データ元: Yahoo Finance（yfinance経由の非公式な取得。仕様変更で止まることがある）
注意: breadth.csv は「現在の」S&P500構成銘柄で過去を計算するため、
      生存者バイアスがある（公式の騰落統計ではなくproxy）。
"""
import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

import pandas as pd
import yfinance as yf

SP500_URL = (
    "https://raw.githubusercontent.com/datasets/"
    "s-and-p-500-companies/main/data/constituents.csv"
)
ADJ_BASIS = "split_adjusted_ex_dividends(yahoo_close)"
BREADTH_SOURCE = "computed:sp500_current_constituents:yahoo"
FIELDS = {
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Adj Close": "adj_close",
    "Volume": "volume",
}
COLUMNS = ["date", "ticker", "open", "high", "low", "close", "adj_close", "volume", "adj_basis"]
JST = timezone(timedelta(hours=9))


def read_tickers(path):
    """tickers.txt を読む。# 以降はコメント、空行は無視、重複は除去。"""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            s = line.split("#")[0].strip().upper()
            if s:
                out.append(s)
    return list(dict.fromkeys(out))


def _format_one(sub, ticker):
    sub = sub.rename(columns=FIELDS)
    keep = [c for c in ["open", "high", "low", "close", "adj_close", "volume"] if c in sub.columns]
    sub = sub[keep]
    if "close" not in sub.columns:
        return pd.DataFrame(columns=COLUMNS)
    sub = sub.dropna(subset=["close"])
    if sub.empty:
        return pd.DataFrame(columns=COLUMNS)
    idx = pd.to_datetime(sub.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    sub = sub.copy()
    sub.index = idx.strftime("%Y-%m-%d")
    sub.index.name = "date"
    sub = sub.reset_index()
    sub.insert(1, "ticker", ticker)
    sub["adj_basis"] = ADJ_BASIS
    for c in COLUMNS:
        if c not in sub.columns:
            sub[c] = pd.NA
    return sub[COLUMNS]


def to_long(df, tickers):
    """yfinanceの横長の表を、1行＝1銘柄1日の縦長の表に変える。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=COLUMNS)
    parts = []
    if isinstance(df.columns, pd.MultiIndex):
        level0 = set(df.columns.get_level_values(0))
        for t in tickers:
            if t in level0:
                parts.append(_format_one(df[t], t))
    elif len(tickers) == 1:
        parts.append(_format_one(df, tickers[0]))
    parts = [p for p in parts if not p.empty]
    if not parts:
        return pd.DataFrame(columns=COLUMNS)
    return pd.concat(parts, ignore_index=True)


def download(tickers, period, chunk=40, retries=3, pause=3.0):
    """少しずつ分けて取得し、失敗したら待って再試行する。"""
    frames, failed = [], []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        df = None
        for attempt in range(retries):
            try:
                df = yf.download(
                    part,
                    period=period,
                    interval="1d",
                    auto_adjust=False,
                    actions=False,
                    group_by="ticker",
                    threads=True,
                    progress=False,
                )
                break
            except Exception as e:  # 通信エラーや一時的な制限
                print(f"retry {attempt + 1}/{retries}: {e}", file=sys.stderr)
                time.sleep(pause * (attempt + 1))
        long = to_long(df, part)
        got = set(long["ticker"]) if not long.empty else set()
        failed += [t for t in part if t not in got]
        if not long.empty:
            frames.append(long)
        time.sleep(pause)
    data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    return data, failed


def load_sp500():
    """S&P500構成銘柄の一覧を取得し、Yahooの表記（BRK.B→BRK-B）に直す。"""
    with urllib.request.urlopen(SP500_URL, timeout=30) as r:
        df = pd.read_csv(r)
    syms = df["Symbol"].astype(str).str.strip().str.upper().str.replace(".", "-", regex=False)
    return list(dict.fromkeys(syms))


def compute_breadth(px, keep_days=260):
    """S&P500構成銘柄の日足から、市場内部の指標を日ごとに計算する。"""
    close = px.pivot(index="date", columns="ticker", values="close").sort_index().astype(float)
    high = px.pivot(index="date", columns="ticker", values="high").sort_index().astype(float)
    low = px.pivot(index="date", columns="ticker", values="low").sort_index().astype(float)

    sma50 = close.rolling(50, min_periods=50).mean()
    sma200 = close.rolling(200, min_periods=200).mean()
    hi252 = high.rolling(252, min_periods=252).max()
    lo252 = low.rolling(252, min_periods=252).min()
    prev = close.shift(1)

    def pct(cond, valid):
        n = valid.sum(axis=1)
        return (cond & valid).sum(axis=1) / n.where(n > 0) * 100

    v50, v200, v252 = sma50.notna(), sma200.notna(), hi252.notna()
    vchg = close.notna() & prev.notna()
    series = {
        "sp500_pct_above_50sma": pct(close > sma50, v50 & close.notna()),
        "sp500_pct_above_200sma": pct(close > sma200, v200 & close.notna()),
        "sp500_new_52w_highs": ((high >= hi252) & v252).sum(axis=1).where(v252.sum(axis=1) > 0),
        "sp500_new_52w_lows": ((low <= lo252) & lo252.notna()).sum(axis=1).where(lo252.notna().sum(axis=1) > 0),
        "sp500_advancers": ((close > prev) & vchg).sum(axis=1).where(vchg.sum(axis=1) > 0),
        "sp500_decliners": ((close < prev) & vchg).sum(axis=1).where(vchg.sum(axis=1) > 0),
        "sp500_members_with_data": close.notna().sum(axis=1),
    }
    wide = pd.DataFrame(series)
    wide = wide.dropna(subset=["sp500_pct_above_200sma", "sp500_new_52w_highs"]).tail(keep_days)
    out = wide.reset_index().melt(id_vars="date", var_name="series", value_name="value")
    out["value"] = out["value"].astype(float).round(2)
    out["source"] = BREADTH_SOURCE
    return out.sort_values(["date", "series"]).reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out")
    ap.add_argument("--tickers", default="tickers.txt")
    ap.add_argument("--no-breadth", action="store_true")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    started = datetime.now(timezone.utc)

    # 1) 分析対象の日足
    tickers = read_tickers(a.tickers)
    px, failed = download(tickers, "3y")
    if px.empty:
        print("データを1件も取得できませんでした。", file=sys.stderr)
        sys.exit(1)
    px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
    px.to_csv(os.path.join(a.out, "prices_daily.csv.gz"), index=False, compression="gzip")
    last_by_ticker = px.groupby("ticker")["date"].max()
    as_of = last_by_ticker.max()
    stale = sorted(last_by_ticker[last_by_ticker < as_of].index.tolist())

    # 2) 市場内部（失敗しても日足は保存する）
    breadth = {"status": "skipped"}
    if not a.no_breadth:
        try:
            members = load_sp500()
            bpx, bfailed = download(members, "2y")
            b = compute_breadth(bpx)
            b.to_csv(os.path.join(a.out, "breadth.csv"), index=False)
            breadth = {
                "status": "ok",
                "as_of": b["date"].max() if not b.empty else None,
                "members_listed": len(members),
                "members_failed": len(bfailed),
                "source": BREADTH_SOURCE,
                "note": "現在の構成銘柄で過去を計算（生存者バイアスあり、proxy）",
            }
        except Exception as e:
            breadth = {"status": "failed", "error": str(e)[:300]}

    # 3) 実行記録
    finished = datetime.now(timezone.utc)
    manifest = {
        "generated_utc": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_jst": finished.astimezone(JST).strftime("%Y-%m-%d %H:%M JST"),
        "runtime_sec": round((finished - started).total_seconds()),
        "source": "Yahoo Finance via yfinance (unofficial)",
        "yfinance_version": yf.__version__,
        "prices": {
            "file": "prices_daily.csv.gz",
            "as_of": as_of,
            "price_basis": ADJ_BASIS,
            "rows": int(len(px)),
            "tickers_requested": len(tickers),
            "tickers_ok": int(px["ticker"].nunique()),
            "tickers_failed": failed,
            "tickers_stale": stale,
        },
        "breadth": breadth,
    }
    with open(os.path.join(a.out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))

    # 3割以上取れなかったら失敗扱い（前回のデータを残し、GitHubから失敗メールが届く）
    if len(failed) > 0.3 * len(tickers):
        sys.exit(2)


if __name__ == "__main__":
    main()
