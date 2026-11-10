#!/usr/bin/env python3
"""保管ページ「米国株指標チェック」（前回の状態と直近180日の本文の置き場）に入れる文書を作る。

書き手が書いた控え（<report_id>.save.json）とレポート本文（Markdown）から、F9 の形式の3つの文書を作る。
  reports/<id>          … 一覧用の情報（控えの meta をそのまま）
  report_bodies/<id>    … 本文（Markdown のまま）と state_yaml
  state/latest_daily または state/latest_weekly … 最新の状態（report_id、基準日、作成、yaml）
  state/scenarios       … シナリオの履歴（控えに scenarios があるときだけ。週次）

使い方:
  python3 tools/state_docs.py /home/claude/out/20261110-DAILY.save.json reports/daily/20261110-DAILY.md --mode daily
  python3 tools/state_docs.py /home/claude/out/20261113-WEEKLY.save.json reports/weekly/20261113-WEEKLY.md --mode weekly_standard

mode：daily／daily_backfill／weekly_standard／weekly_full。
  --no-latest を付けると state/latest_* を作らない（補完した日・週より新しいレポートがすでにあるとき）。
出力先は控えと同じ場所。標準出力に、ArtifactData の batch に渡す writes を JSON で出す。
state/latest_*・state/scenarios はすでにある文書なので、読んだときの version を if_version に付けて書く（このスクリプトは付けない）。
本文は変えない。どこにも送らない。
"""
import argparse
import json
import os
import sys

MAX_BODY_BYTES = 200_000  # F9：本文が200KBを超える場合は truncated=true


def main():
    ap = argparse.ArgumentParser(description="保管ページ（state DB）に入れる3つの文書を作る")
    ap.add_argument("save_json")
    ap.add_argument("report_md")
    ap.add_argument("--mode", required=True, choices=["daily", "daily_backfill", "weekly_standard", "weekly_full"])
    ap.add_argument("--no-latest", action="store_true")
    a = ap.parse_args()

    with open(a.save_json, encoding="utf-8") as f:
        save = json.load(f)
    meta = dict(save.get("meta") or {})
    state_yaml = save.get("state_yaml") or ""
    with open(a.report_md, encoding="utf-8") as f:
        body = f.read()

    rid = meta.get("report_id") or meta.get("id") or os.path.basename(a.report_md)[:-3]
    problems = []
    for k in ("as_of_market_et", "created_jst", "headline", "tape_regime"):
        if not meta.get(k):
            problems.append("meta.%s が空です" % k)
    if os.path.basename(a.report_md)[:-3] != rid:
        problems.append("report_id（%s）とファイル名（%s）が違います" % (rid, os.path.basename(a.report_md)))
    if not state_yaml.strip():
        problems.append("state_yaml が空です")
    if problems:
        sys.exit("控え（save.json）を直してください：" + "／".join(problems))

    meta.update({"id": rid, "report_id": rid, "mode": a.mode, "body_pruned": False})
    meta.setdefault("github_path", ("reports/weekly/" if a.mode.startswith("weekly") else "reports/daily/") + rid + ".md")

    truncated = len(body.encode("utf-8")) > MAX_BODY_BYTES
    if truncated:
        cut = body.encode("utf-8")[:MAX_BODY_BYTES].decode("utf-8", errors="ignore")
        k = cut.rfind("\n\n")
        body = (cut[:k] if k > MAX_BODY_BYTES // 8 else cut) + "\n\n（本文が長いため、ここまでを保存しています。完全版は GitHub の " + meta["github_path"] + "）\n"

    out_dir = os.path.dirname(os.path.abspath(a.save_json))
    docs = [("reports", rid, meta), ("report_bodies", rid, {"id": rid, "body_md": body, "truncated": truncated, "state_yaml": state_yaml})]
    if not a.no_latest:
        latest = "latest_weekly" if a.mode.startswith("weekly") else "latest_daily"
        docs.append(("state", latest, {"report_id": rid, "as_of_market_et": meta["as_of_market_et"], "created_jst": meta["created_jst"], "yaml": state_yaml}))
    sc = save.get("scenarios")
    if sc is not None:
        if not isinstance(sc, dict) or not isinstance(sc.get("items"), list) or not sc.get("updated_jst"):
            sys.exit("控え（save.json）を直してください：scenarios は {updated_jst, items（配列）} の形にします")
        docs.append(("state", "scenarios", {"updated_jst": sc["updated_jst"], "items": sc["items"][-60:]}))
    elif a.mode.startswith("weekly"):
        sys.exit("控え（save.json）を直してください：週次では scenarios（シナリオの履歴）が要ります")
    writes = []
    for coll, doc_id, data in docs:
        path = os.path.join(out_dir, "%s.%s.json" % (rid if coll != "state" else doc_id, coll))
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        w = {"op": "set", "collection": coll, "doc_id": doc_id, "file_path": path}
        if coll == "state":
            w["if_version"] = "<読んだときの version>"
        writes.append(w)
    print(json.dumps({"report_id": rid, "truncated": truncated, "writes": writes}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
