#!/usr/bin/env python3
"""読むための保管ページ（Artifact）に入れる文書を、reports/ の Markdown から作る。

レポートを1本ずつ、次の2つの JSON ファイルにする（保管ページのデータベースにそのまま入る形）。
  <id>.meta.json … 一覧に出す小さな情報（id、基準日 as_of、見出し headline、タグ tags、文字数）
  <id>.body.json … 本文（Markdown のまま）

使い方（リポジトリの中のどこからでも）:
  python3 tools/archive_doc.py us_daily --recent 7      # 米国株 日次（market-data の reports/daily/）
  python3 tools/archive_doc.py us_weekly --recent 4     # 米国株 週次（market-data の reports/weekly/）
  python3 tools/archive_doc.py semis_daily --recent 7   # 半導体 日次（semis-data の reports/daily/）
  python3 tools/archive_doc.py semis_weekly --recent 4  # 半導体 週次（semis-data の reports/）
  python3 tools/archive_doc.py us_daily --all           # 全部
  python3 tools/archive_doc.py us_daily --id 20261109-DAILY

出力先は既定でリポジトリの隣の archive_docs/<種類>/（リポジトリの外。git add -A に巻き込まれない）。
標準出力に、作ったファイルの一覧を JSON で出す。保管ページへの書き込みそのものは、
手順書のとおり ArtifactData で行う（このスクリプトはファイルを作るだけで、どこにも送らない）。
レポートの中身は変えない。数字の計算もしない。
"""
import argparse
import json
import os
import re
import sys

KINDS = {
    "us_daily": {"repo": "market-data", "dir": "reports/daily", "pattern": r"^(\d{4})(\d{2})(\d{2})-DAILY\.md$"},
    "us_weekly": {"repo": "market-data", "dir": "reports/weekly", "pattern": r"^(\d{4})(\d{2})(\d{2})-WEEKLY\.md$"},
    "semis_daily": {"repo": "semis-data", "dir": "reports/daily", "pattern": r"^(\d{4})-(\d{2})-(\d{2})\.md$"},
    "semis_weekly": {"repo": "semis-data", "dir": "reports", "pattern": r"^(\d{4})-(\d{2})-(\d{2})\.md$"},
}

# 1つの文書は 256 KiB まで。余裕を見て、これを超える本文は段落の切れ目で切る（全文は GitHub にある）。
MAX_BODY_BYTES = 230_000
CUT_BODY_BYTES = 200_000
HEADLINE_CHARS = 170


def strip_md(s):
    """一覧に出す1行にするため、Markdown の飾りを外す。"""
    s = re.sub(r"`([^`]*)`", r"\1", s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = s.replace("*", "").replace("__", "")
    return re.sub(r"\s+", " ", s).strip()


def clip(s, n=HEADLINE_CHARS):
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


META_PREFIX = re.compile(
    r"^[-*・\s]*\**(作成|基準日|市場基準日|市場基準|データ|データ生成|次の米国取引|モード|前回の週次|定義変更|仕様|初回)"
)


def headline_of(text):
    """見出し：『今日のひとこと』の最初の行 → 『一文結論』 → 『今回の結論』 → 最初の本文の段落、の順で探す。"""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if re.match(r"^#{1,4}\s*今日のひとこと", ln.strip()):
            for nxt in lines[i + 1:]:
                t = nxt.strip()
                if not t:
                    continue
                if t.startswith("#"):
                    break
                return clip(strip_md(re.sub(r"^[-*・]\s*", "", t)))
            break
    for ln in lines[:80]:
        m = re.search(r"(一文結論|今回の結論)\s*[：:]\s*(.+)", ln)
        if m:
            return clip(strip_md(m.group(2)))
    for ln in lines[:80]:
        t = ln.strip()
        if not t or t.startswith("#") or t.startswith("|") or t.startswith("---") or t.startswith(">"):
            continue
        if META_PREFIX.match(t):
            continue
        return clip(strip_md(re.sub(r"^[-*・]\s*", "", t)))
    return ""


def created_of(text):
    """本文に書かれた作成日時（日本時間）。書かれていなければ None。"""
    head = "\n".join(text.splitlines()[:12])
    m = re.search(r"(作成|本判定)\s*[：:]?\s*\**\s*(\d{4}-\d{2}-\d{2})(?:[（(][^）)]{1,3}[）)])?\s*(\d{1,2}:\d{2})?", head)
    if not m:
        return None
    d, t = m.group(2), m.group(3)
    if t and len(t) == 4:
        t = "0" + t
    return d + (" " + t if t else "")


def tags_of(kind, text):
    head = "\n".join(text.splitlines()[:8])
    tags = []
    if "補完版" in head:
        tags.append("補完版")
    if kind.endswith("weekly") and re.search(r"（Full）|モード\s*[：:]\s*\**\s*Full", head):
        tags.append("Full")
    return tags


def cut_body(text):
    raw = text.encode("utf-8")
    if len(raw) <= MAX_BODY_BYTES:
        return text, False
    part = raw[:CUT_BODY_BYTES].decode("utf-8", errors="ignore")
    k = part.rfind("\n\n")
    if k > CUT_BODY_BYTES // 4:
        part = part[:k]
    return part + "\n", True


def repo_root(start):
    d = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, ".git")):
            return d
        up = os.path.dirname(d)
        if up == d:
            return None
        d = up


def build(kind, path, rel, out_dir):
    spec = KINDS[kind]
    name = os.path.basename(path)
    m = re.match(spec["pattern"], name)
    if not m:
        raise ValueError("ファイル名が %s の形ではありません: %s" % (kind, name))
    doc_id = name[:-3]
    as_of = "%s-%s-%s" % m.groups()
    with open(path, encoding="utf-8") as f:
        text = f.read()
    body, truncated = cut_body(text)
    meta = {
        "id": doc_id,
        "kind": kind,
        "as_of": as_of,
        "headline": headline_of(text),
        "tags": tags_of(kind, text),
        "created_jst": created_of(text),
        "chars": len(text),
        "truncated": truncated,
        "repo": spec["repo"],
        "source": rel,
    }
    os.makedirs(out_dir, exist_ok=True)
    meta_file = os.path.join(out_dir, doc_id + ".meta.json")
    body_file = os.path.join(out_dir, doc_id + ".body.json")
    with open(meta_file, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    with open(body_file, "w", encoding="utf-8") as f:
        json.dump({"id": doc_id, "as_of": as_of, "body_md": body, "truncated": truncated}, f, ensure_ascii=False)
    return {
        "id": doc_id,
        "as_of": as_of,
        "meta_file": meta_file,
        "body_file": body_file,
        "body_bytes": os.path.getsize(body_file),
        "truncated": truncated,
        "headline": meta["headline"],
    }


def main():
    ap = argparse.ArgumentParser(description="保管ページに入れる文書（meta と body の JSON）を reports/ から作る")
    ap.add_argument("kind", choices=sorted(KINDS))
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--recent", type=int, default=7, help="新しい順に何本作るか（既定 7）")
    g.add_argument("--all", action="store_true", help="全部作る")
    g.add_argument("--id", action="append", help="この id だけ作る（複数回指定できる）")
    ap.add_argument("--repo", help="リポジトリの場所（既定：このスクリプトのあるリポジトリ）")
    ap.add_argument("--out", help="出力先（既定：リポジトリの隣の archive_docs/<種類>/）")
    a = ap.parse_args()

    root = os.path.abspath(a.repo) if a.repo else repo_root(os.path.dirname(os.path.abspath(__file__)))
    if not root:
        sys.exit("リポジトリの場所が分かりません。--repo で指定してください。")
    spec = KINDS[a.kind]
    src_dir = os.path.join(root, spec["dir"])
    if not os.path.isdir(src_dir):
        sys.exit("%s がありません（%s は %s のリポジトリで使います）。" % (src_dir, a.kind, spec["repo"]))
    names = sorted(n for n in os.listdir(src_dir) if re.match(spec["pattern"], n))
    if not names:
        sys.exit("%s に %s のレポートがありません（%s は %s のリポジトリで使います）。" % (src_dir, a.kind, a.kind, spec["repo"]))
    if a.id:
        want = set(a.id)
        names = [n for n in names if n[:-3] in want]
        missing = sorted(want - {n[:-3] for n in names})
        if missing:
            sys.exit("見つからない id: " + ", ".join(missing))
    elif not a.all:
        names = names[-max(1, a.recent):]
    out_dir = os.path.abspath(a.out) if a.out else os.path.join(os.path.dirname(root), "archive_docs", a.kind)

    docs = []
    for n in reversed(names):  # 新しい順
        rel = spec["dir"] + "/" + n
        docs.append(build(a.kind, os.path.join(src_dir, n), rel, out_dir))
    print(json.dumps({"kind": a.kind, "out_dir": out_dir, "count": len(docs), "docs": docs}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
