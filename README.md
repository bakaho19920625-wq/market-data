# market-data

米国株データの自動取得・解析（GitHub Actions）と、Claudeプロジェクト「米国株指標チェック」の仕様ファイル。

- main：プログラム（fetch_data.py、analyze.py、tools/archive_doc.py＝読む用の保管ページに入れる文書づくり、tools/check_report.py＝書き上がったレポートの点検、tools/state_docs.py＝保管ページ「米国株指標チェック」に入れる文書づくり）、監視銘柄（tickers.txt）、ナスダック100の一覧（ndx100_constituents.csv。追補09）、仕様（CLAUDE.md、00〜09、daily_runbook.txt、weekly_runbook.txt、runbook/）、レポート本文の全履歴（reports/）、データ更新の起動用（refresh/）
  - 00 本文、01 原典メモ、02 ユーザー設定、03・04・06・07・08・09 追補（定義）、05 やさしい表記の対応表、手順書2本（日次・週次の段取り＝流れ）と runbook/（書き方の共通部分とひな形 daily_write.txt・weekly_write.txt、節ごとの書き方 daily/・weekly/、補完、読む用の保管ページへの保存、変更の履歴 CHANGELOG.txt）
- data：毎朝自動更新される最新データ（signals.json など）と、判定結果の日次履歴 history_signals.csv（1日1行で追記。初回は main の history_seed.csv から）
