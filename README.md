# market-data

米国株データの自動取得・解析（GitHub Actions）と、Claudeプロジェクト「米国株指標チェック」の仕様ファイル。

- main：プログラム（fetch_data.py、analyze.py）、監視銘柄（tickers.txt）、仕様（CLAUDE.md、00〜08、daily_runbook.txt、weekly_runbook.txt）、レポート本文の全履歴（reports/）、データ更新の起動用（refresh/）
  - 00 本文、01 原典メモ、02 ユーザー設定、03・04・06・07・08 追補（定義）、05 やさしい表記の対応表、手順書2本（日次・週次の段取り）
- data：毎朝自動更新される最新データ（signals.json など）と、判定結果の日次履歴 history_signals.csv（1日1行で追記。初回は main の history_seed.csv から）
