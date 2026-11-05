# refresh/ — データ更新の起動用

レポートの定期実行が、データが古いのに GitHub API（gh workflow run）を使えないときに、
`refresh/request.txt` に時刻を書いて main に push する。この push で `.github/workflows/update.yml` が起動する（`on: push` の `paths: refresh/**`）。
GitHub の予約実行は数時間遅れたり来なかったりするため（2026-11-02〜05 に観測）、レポート側から確実に起動するための経路。
中身（時刻）に意味はない。ほかのファイルはここに置かない。
