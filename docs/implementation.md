# 実装・検証の記録

2026-09-17。設計書 v0.6 の初期版として、ローカルの個人用Webアプリを実装した。FastAPI / Jinja2 / HTMX / SQLiteを使用し、Python WorkerがGitHubとGitを操作する。エージェント実行はPi RPC。App Serverは比較案として設計書に残す。

## 実装済み

- [x] リポジトリ登録、Issue起票・取り込み、検索、6列のボード
- [x] 仕様相談、編集、確定、GitHub反映、改訂・同期競合
- [x] ブランチ・worktree、初回commit・push、Draft PR、Piによる編集・テスト
- [x] 独立レビュー、対象SHA・仕様版の固定、指摘の対応・修正・COMMENT投稿
- [x] 工程別・リポジトリ別・タスク別モデル設定、停止、チェックポイント、モデル変更・再開
- [x] 永続ジョブ、Workerとタスクの排他、再起動復旧、結果不明のGitHub操作の照合
- [x] Codex OAuth状態の表示、モデル一覧取得、Providerエラーと停止理由の表示
- [x] Docker内のツール実行、ローカルWeb認証、CSRF・Origin検証、HTMLエスケープ
- [x] マージと未マージcloseの区別、Draft解除、約60秒間隔のGitHub同期
- [x] 自動テスト、実ブラウザ確認、Codex実推論、起動手順、wheel作成

## 検証の範囲

最終実行: 通常テスト20件とブラウザテスト1件が成功。実Codexテスト1件は別途成功（通常実行では明示的にskip）。`ruff check`も成功。

| 対象 | 方法・結果 |
| --- | --- |
| 通常テスト | `uv run pytest -q`。GitHubとモデル応答は模擬実装。実Gitの一時bare repositoryにcommit・pushする |
| ブラウザ | ChromiumでIssue起票→未保存の仕様を確定→実装→別モデルの独立レビュー→ボード表示を操作。1500px / 390px幅、JavaScript例外なし。HTMXとSSEを実通信で確認 |
| 実Codex | Pi 0.85.1、ChatGPT OAuth、`gpt-5.6-luna`。実Dockerで加算関数とunittestを作成・実行、独立レビュー、実装セッション再開後のテスト再実行に成功。GitHub部分は模擬実装 |
| Pi RPC | subprocessによる検証。prompt受付や`agent_end`だけで完了とせず`agent_settled`を待つ。停止時は`clear_queue`→`abort`→プロセス終了 |
| Docker | 読み書き・shell呼び出し、`.git`ポインタの読み取り専用化、ホスト認証ファイル・Dockerソケット非公開、出力上限、コマンドのタイムアウトを実コンテナで確認 |
| 起動 | `kanban serve`でWebとWorkerが起動し、ログイントークン付きURLからボードが開く。Ctrl+Cで終了 |
| 配布物 | `uv build`成功。wheelにテンプレート、静的ファイル、Pi拡張、コンテナ内ヘルパーを含む |

スクリーンショットは模擬データによる実アプリ画面: [ボード](screenshots/board.png) / [レビュー](screenshots/review.png)。初期のポンチ絵は`wireframes/`に残す。

## 受け入れ条件との対応

| 条件 | 確認した内容 |
| --- | --- |
| AC-1 / AC-2 | Issue起票、本文同期、編集中の仕様保持、競合時の確定拒否、仕様改訂後のworktree・同じPRへの反映 |
| AC-3 / AC-4 | 実Gitによるbranch・Draft PRフロー、テスト結果保存、工程別モデル、レビューの別セッションと固定SHA |
| AC-5 | 設定のスナップショット、二重実行拒否、別Providerへの再開、未commitファイルと特定Piセッションの保持。実セッションの復元もCodexで確認 |
| AC-6 / AC-7 | 指摘修正で同じPRを更新。head・base・仕様版が変わったレビューは修正・投稿に使わない。待機中のPR更新も再確認する |
| AC-8 | Issue / PR作成後の応答喪失を再現し、操作IDから既存リソースを見つけて重複作成を防ぐ。明示的な権限拒否後は設定修正して再試行可能 |
| AC-9 | Worker再起動で実行中Runを中断扱いにし、未commitファイルとチェックポイントを残して再開。作業フォルダ消失時は親ディレクトリのGitを操作せず停止 |
| AC-10 / AC-12 | 模擬的な利用上限エラー・モデル取り違えでRunを失敗にし、成果物を保持。自動Provider変更なし。実CodexのOAuthログインは確認済み |
| AC-11 | マージのみ完了に移し、未マージcloseは中止・要確認として残す |
| AC-13（追加段階） | PiのProvider・モデル設定を工程ごとに指定できる。別Providerへの引き継ぎは模擬実装で確認 |

## 未検証・初期版の制約

- **実GitHubへの書き込みは未検証。** ユーザーからテスト用リポジトリはないとの回答を受け、一時GitリポジトリとGitHub HTTPの模擬応答を使用した。後から設定された開発用remoteには検証用Issue・PRを作成していない。
- **OpenRouter・llama.cppの実推論は未検証。** Piの接続設定とモデル一覧を利用する経路を実装し、Providerを変える再開を模擬テストで確認した。
- Codexの実アカウントの利用枠を意図的に使い切る試験は行っていない。制限到達は模擬エラーで検証。残り枠・リセット時刻はPiが返すエラー以上の情報を推測しない。
- Pi認証はターミナルの`/login`を使う。Web内のOAuthログイン画面は実装していない。PiのバージョンはREADMEで0.85.1を指定する。
- Workerは1プロセスでジョブを順次処理する。長いRun中はGitHub同期が待機する。複数Worker・Windows向け実行制御・チーム利用は対象外。
- 開発コンテナはネットワークなし。依存パッケージは利用者がイメージへ組み込む。既定イメージにはPython標準ライブラリのみを含む。
- 作業フォルダ消失時のアーカイブ自動展開、ログ・セッション・チェックポイントの自動削除、金額予算による停止は未実装。
- GitHub本文の競合確認は更新直前の読取比較。読取と更新の間に外部編集が入る競合を、GitHub全体をロックして防ぐものではない。
- HTTPテスト時にStarlette由来の非推奨警告2件が出る。アプリ・テストの失敗はない。

## 構成と責務

| ファイル | 責務 |
| --- | --- |
| `kanban/web.py`、`templates/`、`static/` | ローカル認証、画面・API・SSE |
| `kanban/workflow.py` | 工程、仕様版、モデル設定、Run、レビューの整合性 |
| `kanban/worker.py`、`db.py` | ジョブ永続化、排他、復旧、定期同期 |
| `kanban/github.py`、`workspaces.py` | GitHub操作と照合、Gitのworktree・commit・push・保存 |
| `kanban/pi.py` | Pi RPCの開始・終了・イベント・セッション再開 |
| `kanban/sandbox.py`、`agent/` | Docker実行環境とPi用の作業ツール |

実行・設定手順は[README](../README.md)を参照。
