# Agent Cockpit

[![test](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml/badge.svg)](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

> [herdr](https://herdr.dev) 上で動く CLI コーディング agent のための、ブラウザ版コントロールコクピット。
> すべての agent の状態を一目で把握し、ライブ端末への接続、プロンプト送信、
> スクリーンショットのアップロード、fleet のオーケストレーションを——PC とスマホのブラウザから。

[中文](README.md) | [English](README.en.md)

<p align="center">
  <img src="docs/screenshots/board-desktop.png" alt="ボード(PC)" width="74%">
  <img src="docs/screenshots/board-mobile.png" alt="ボード(スマホ)" width="22%">
</p>

[Orca](https://onorca.dev) の Agent Dashboard に着想を得た、既存の Herdr セッションに
そのまま接続できる軽量 Web アプリです。
[Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) 連携は任意です。

## 機能一覧

- **ボード** — すべての herdr セッションの coding agent(codex / kimi / claude / qoder / grok / opencode)を *対応必要 / 実行中 / 完了 / 待機* の列にリアルタイム表示。
- **ライブ端末** — agent カードをクリックして端末出力を表示。xterm.js でプロンプト送信、コマンド実行、特殊キー送信が可能。
- **スクリーンショット → agent** — 画像をアップロードすると `@/path` として自動挿入され、agent がその画像を「見て」作業できます。
- **対応 Inbox** — 停止中の agent、失敗したバックグラウンドタスク、レビュー待ち diff、Agent Mail 未読を 1 つのキューに集約。
- **Web Push** — Inbox で有効化すると、通知から該当する pane / タスク / メッセージへ直接ジャンプ。
- **agent 間メッセージ** — Agent Mail ベース:メッセージの送受信と既読確認。Agent Mail がなくてもメッセージ画面が隠れるだけで、他の機能は動作します。
- **ファイルブラウザ + エディタ** — サンドボックス化されたホワイトリスト内でプロジェクトファイルの閲覧・編集・ダウンロード・アップロード。
- **codex バックグラウンドタスク** — `codex exec` ジョブの起動、出力のストリーミング、diff レビュー、変更の適用/stash。
- **モバイル対応** — レスポンシブな単一ファイルフロントエンド。カメラアップロード、タッチ操作、PWA としてホーム画面に追加可能。
- **ダーク / ライトテーマ** — ヘッダーで切り替え、セッションをまたいで記憶。ライトモードでは TUI が描く明示的な暗色(opencode の黒背景など)を自動で反転し、読みやすい配色にします。

## 仕組み

```
ブラウザ(PC / スマホ)
    │  LAN / VPN(:8790)
    ▼
Agent Cockpit(FastAPI、herdr + hub と同じホスト)
    ├── Agent Mail SQLite を読み取り   (読み取り専用、WAL)
    ├── Agent Mail hub MCP 経由で書き込み(送信 / 既読)
    ├── herdr ソケットを読み取り(全セッション)(pane 状態 / 出力)
    └── SSE でブラウザに状態と diff をプッシュ
```

**herdr と同じマシン**にデプロイし、すべてローカルで読み取るため遅延ゼロ。
PC とスマホはブラウザクライアントに過ぎません。Agent Mail がない場合は
メッセージ関連ビューのみ非表示。hub が一時的に落ちていても既存メッセージは
読み取り専用で表示され、ボード、端末、ファイル、タスク、Inbox、プッシュ通知は
引き続き動作します。

## インストール

### 前提条件

| 依存 | 用途 |
| --- | --- |
| [herdr](https://herdr.dev) | このコクピットが可視化・操作する agent セッション |
| [Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) hub(`:8765`、任意) | Inbox とメッセージビューに agent 間メッセージを追加 |
| `codex` CLI(認証済み) | バックグラウンド `codex exec` タスク用 |
| Python 3.12+ | ランタイム |

### ワンコマンドインストール

```bash
curl -fsSL https://raw.githubusercontent.com/fyc0451/agent-cockpit/main/install.sh | bash
```

インストーラは `~/agent-cockpit` にクローンし、仮想環境の作成と依存の
インストールを行い、Linux では `agent-cockpit.service`、macOS では
LaunchAgent を登録します。起動に失敗したら `~/agent-cockpit/doctor.sh` を
実行してください。同梱の Agent Mail 補助コマンドは `~/.local/bin` に安全に
リンクされ、既存の通常ファイルやユーザー独自のシンボリックリンクは上書きしません。

### 手動インストール

```bash
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python server.py
# → http://localhost:8790
```

LAN/VPN の他のデバイスからアクセスするには、`.env.example` を `.env` にコピーし、
`COCKPIT_HOST=0.0.0.0` とランダムな `COCKPIT_TOKEN` を設定します。token が
ない場合、サーバは非ループバックアドレスへのバインドを拒否します。

> **セキュリティ警告:** リモートアクセスには HTTPS か Tailscale Serve を使って
> ください。平文 HTTP ではログイン cookie が同一ネットワークの第三者に見える
> 可能性があります。Agent Cockpit を直接公衆インターネットに公開しないでください。

### systemd サービスとしてデプロイ

```bash
loginctl enable-linger "$USER"   # ログアウト後も user service を維持
cp agent-cockpit.service ~/.config/systemd/user/agent-cockpit.service
# 環境に合わせてパスを編集してから:
systemctl --user daemon-reload
systemctl --user enable --now agent-cockpit
```

`KillMode=process` により、コクピットを再起動しても独立した Herdr セッションは
維持されます。ただしブラウザから作成した PTY は切断されることがあるため、
永続ジョブとして扱わないでください。

手動起動時は先に `.env` を読み込みます:

```bash
set -a; source .env; set +a
.venv/bin/python server.py
```

## 初回利用(5 分クイックスタート)

1. ブラウザで `http://localhost:8790` を開きます。
2. ボードが空なのは正常です。空状態の **🚀 最初のワークスペースを作成**
   (または「セッション」ページの **+ ワンクリックワークスペース**)をクリックし、
   セッション名、プロジェクトディレクトリ、起動する agent(例:`codex,kimi`)を
   入力して起動します。セッションが存在しなければ自動作成されます。
3. **ボード**に戻ると、agent が状態別の列に自動で並びます。カードをクリックで
   出力を表示、カードの 🖥 で TUI を接管できます。
4. **対応(Inbox)**ページでは停止/失敗した agent をまとめて処理でき、
   ブラウザ通知を有効化できます。
5. 問題が起きたら「設定 → 環境チェック」へ:herdr、各 agent の実行ファイル、
   Agent Mail の準備状況が一目で分かります。CLI では `./doctor.sh` が同じ役割です。

## 使い方

### ボード

- 4 つのライブ列:**⚠ 対応必要 / ⚡ 実行中 / ✓ 完了 / ○ 待機**。
- カードをクリックで pane の「フロー」ビューへ。カード右上の **🖥** で TUI を接管。
- 下部の **🚀 起動バー**:既存セッション + agent 種別 + 作業ディレクトリを選び
  **+ 新規エージェント**。セッションが 1 つもない場合はワークスペース作成へ誘導します。

### 対応(Inbox)

- 入力待ちの agent、失敗したバックグラウンドタスク、レビュー待ち diff、
  Agent Mail 未読を 1 つのキューに集約。
- 項目をクリックで処理箇所へジャンプ。**通知を有効化**で Web Push を購読。
- プッシュにはセキュアコンテキストが必要です:`https://`(Tailscale Serve など)
  または `http://localhost`。iPhone/iPad では先に Safari で
  **共有 → ホーム画面に追加**し、ホーム画面のアイコンから開いて通知を有効化
  してください(iOS は通常の Safari タブからの Web Push 許可を認めていません)。

### 端末

- **+ 新規ターミナル**でブラウザ PTY を開きます(サーバ再起動で切断されます。
  永続ジョブには使わないでください)。
- ツールバー:📎 アップロード(画像/ファイルを `@/path` として自動挿入)、
  @連携(agent が連絡できる相手の情報を挿入)、🖥 herdr(herdr セッションに
  attach して分割画面で操作)、📜 フロー表示、📋 クリップボードへコピー。
- herdr キー:`Ctrl-b` で pane 切替 / `d` でデタッチ / `?` で全ショートカット。
- スマホでは **⌨ キーボード**で方向キー / Ctrl キーと可視入力ボックスを展開。

### フロー(herdrflow)

- agent pane ごとに 1 パネル:スクロール・コピー可能な出力と、クイック指示用の
  入力欄。
- `prompt` モードは agent のプロンプトインターフェース経由、`send` モードは
  キー入力を直接シミュレートします。
- 📋 で Herdr TUI でコピーした文字列を入力欄に挿入。⛶ で 1 つのワークスペースを
  全画面表示。

### セッション

- すべての herdr セッションを一覧表示:pane のクリーン再起動 / resume 再起動、
  停止、停止済みセッションの削除。
- **+ ワンクリックワークスペース**:セッション作成 → pane 分割 → agent 起動を
  自動化。Agent Mail があれば identity 登録と通知も行います。**📧 通信初期化**で
  セッション内の全 agent の agent-mail identity を一括登録できます。
- 各セッションは 1 つの Agent Mail プロジェクトを永続化します。同じ clone の
  linked worktree は main worktree に統合し、曖昧な旧セッションは最初の pane cwd
  から推測せず、選択を求めます。

### メッセージ

- プロジェクト / agent 別に Agent Mail を閲覧、送信、既読確認。
- Agent Mail 未インストールや hub オフライン時は自動で縮退:既存メッセージは
  読み取り専用のまま、他機能は影響を受けません。

### ファイル

- 上部の「アクセス可能ディレクトリ」はホワイトリストのルートです:システム
  ディレクトリ + 登録プロジェクト + カスタムディレクトリ。**＋ ディレクトリ追加**
  で任意のディレクトリをホワイトリストに追加できます(閲覧許可のみ、データは
  移動しません)。
- ディレクトリをクリックで潜り、ファイルをクリックで表示。テキストファイルは
  その場で編集・保存でき、その他は ⬇️ でダウンロード。
- 検索は現在のディレクトリ以下のファイル名を再帰的にマッチします。
- **🚀 ここでワークスペース**:ファイル一覧の現在ディレクトリでワンクリック
  ワークスペースのダイアログを事前入力します。

### 設定

- **表示言語**:中文 / English / 日本語。**外観**:ダーク / ライト(ライトモード
  では TUI が描く明示的な暗色背景を輝度反転するため、opencode の黒背景も
  読みやすく表示されます)。
- **端末フォントサイズ**:10–24。このデバイスのみに適用、即時反映。
- **ディレクトリ別デフォルト agent**:起動バーに作業ディレクトリを入力すると
  自動で预选します。
- **有効な agent**:起動メニューにはチェックした種別のみ表示。
- **実行パラメータ**:アップロード上限、最大ターミナル数、アイドル端末の回収時間、
  端末書き込みタイムアウト。
- **環境チェック**:herdr / 各 agent 実行ファイル / Agent Mail の準備状況。
  ❌ は未インストールです。

## 設定

環境変数で設定します(`.env.example` 参照):

| 変数 | デフォルト | 説明 |
| --- | --- | --- |
| `COCKPIT_HOST` | `127.0.0.1` | バインドアドレス |
| `COCKPIT_PORT` | `8790` | ポート |
| `COCKPIT_TOKEN` | 空 | 共有ログイン token。非ループバックバインド時は必須 |
| `HERDR_BIN` | 自動検出 | herdr バイナリのパス |
| `CODEX_BIN` | 自動検出 | codex バイナリのパス |
| `AGENT_MAIL_DB_PATH` | 自動検出 | Agent Mail `storage.sqlite3` のカスタムパス |
| `COCKPIT_VAPID_SUBJECT` | `mailto:agent-cockpit@localhost` | Web Push VAPID contact claim |
| `COCKPIT_VAPID_PRIVATE_KEY` / `PUBLIC_KEY` | 自動生成 | マルチインスタンス環境で固定する VAPID キーペア |

Agent Mail DB は新しい `~/.local/share/mcp_agent_mail/` と従来の
`~/mcp_agent_mail/` を順に検出します。hub token は `~/.agent-mail/client.env`
から自動で読み取られます。ハードコードしないでください。VAPID キーは
`~/dashboard-data/` に一度だけ生成され、
リポジトリには入りません。ユーザー設定は `~/dashboard-data/settings.json` に、
端末フォントサイズなどのデバイス固有の設定はブラウザの localStorage に保存されます。
通信プロジェクトの紐付けは `~/dashboard-data/mail-projects.json` に保存され、identity
token は含みません。

## アップグレード、診断、アンインストール

```bash
./upgrade.sh       # 退役(fail-closed)：ワンクリック更新を無効化、管理されたリリース手順のみ
./doctor.sh        # Python、依存、herdr、Agent Mail、認証、サービスをチェック
./uninstall.sh     # user service のみ削除。コード・設定・データは保持
```

テストの実行:`.venv/bin/pip install -r requirements-dev.txt` の後に
`.venv/bin/pytest -q`

## プロジェクト構成

```
agent-cockpit/
├── server.py              FastAPI アプリ:ルーティング、SSE、静的配信
├── db.py                  hub の SQLite への読み取り専用クエリ
├── herdr_client.py        マルチセッション herdr CLI ラッパー(ボードのデータ源)
├── tasks.py               codex exec タスクランナー + diff/apply
├── files.py               サンドボックス化されたファイルブラウザ/エディタ
├── hub_client.py          MCP 書き込みプロキシ(send_message / ack)
├── web_push.py            VAPID キー、購読、プッシュ配信
├── uploads.py             ファイル/スクリーンショットのアップロード
├── settings.py            ユーザー設定ストレージ
├── terminal.py            ブラウザ PTY 端末
├── static/index.html      単一ファイルフロントエンド(ボード + Inbox + 端末 + タブ)
├── static/sw.js           Web Push service worker とディープリンク
├── static/manifest.webmanifest  PWA メタデータ
├── tests/                 リグレッション/セキュリティテスト
├── install.sh / upgrade.sh（退役） / doctor.sh / uninstall.sh
├── agent-cockpit.service  systemd user ユニットテンプレート
└── launchd.sh / agent-cockpit.plist  macOS LaunchAgent
```

## なぜ CLI ではなくコクピットなのか?

CLI agent(codex、kimi、qoder)は強力ですが、お互いを見ることができません。
herdr はそれらを観察可能な pane に入れてくれますが、そのマシンの端末からしか
見られません。Agent Cockpit はこのローカル端末ビューを **Web コクピット**に
変えます。ソファでスマホを開き、どの agent が止まっているか確認し、バグの
スクリーンショットを投げ、適切な agent に引き継がせることができます。

## 制限事項

- **GUI agent(ZCode Desktop など)はボードに参加できません** — このコクピットが
  操作するのは herdr 配下の*端末* CLI agent です。GUI アプリにはプログラム的な
  操作面がありません。
- **共有 token 認証** — 信頼できる個人 LAN/VPN 向けです。多ユーザー認可体系では
  ないため、ファイアウォールまたはプライベートネットワークの内側に置いてください。
- **通信の安全性** — HTTP はセッション cookie を保護しません。完全に信頼できる
  ネットワーク以外では HTTPS か Tailscale Serve を使ってください。
- **Agent Mail は任意連携** — インストールされている場合、Cockpit はその SQLite を
  直接読み取りますが書き込みは行いません。書き込みはすべて hub MCP API 経由です。
  Agent Mail がない/利用不可の場合はメッセージ関連機能のみ自動で縮退します。

## 貢献とセキュリティ

開発手順は [CONTRIBUTING.md](CONTRIBUTING.md)、脆弱性の非公開報告とデプロイの
脅威モデルは [SECURITY.md](SECURITY.md) を参照してください。コミュニティ行動規範は
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md) に従います。

## License

[MIT](LICENSE)
