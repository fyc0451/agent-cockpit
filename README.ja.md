# Agent Cockpit

[![test](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml/badge.svg)](https://github.com/fyc0451/agent-cockpit/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/)

> [herdr](https://herdr.dev) 上で動く CLI コーディング agent のための、ブラウザ版コントロールコクピット。
> すべての agent の状態を一目で把握し、ライブ端末への接続、プロンプト送信、
> スクリーンショットのアップロード、fleet のオーケストレーションを——PC とスマホのブラウザから。

[中文](README.md) | [English](README.en.md)

## 現在の製品: Cockpit 3.0

Cockpit 3.0 は `http://127.0.0.1:8790/#/chat` のグループチャットです。
UI は瀑布流・メンバー欄・入力欄で、旧ボードではありません。製品ラインは 3.0 と
計画中の 4.0 だけです。2.0 / 3.5 のインストール入口はありません。

現在の入口は `install.sh` です。`web/dist` をビルドし、
`scripts/dev_server.py` で 3.0 を起動します。旧ボードはインストール結果ではありません。

## 機能一覧

- **グループチャット瀑布流** — herdr 上の CLI agent がメンバーとして出る。結論は吹き出し、過程は折りたたみ。
- **既定はキュー** — Enter は空き待ち。手元の作業を止めるときだけ「打断」。
- **Harvest** — pane が `idle` / `done` のときだけ結論を取る。
- **ファイルと添付** — チャットからリポジトリを見てパスをコピー。添付は既定で折りたたみ。
- **設定** — 外観、ソース一括アップグレード、環境チェック。
- **モバイル** — 同じ Hash ルートのチャットをスマホブラウザで開ける。

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

**herdr と同じマシン**にデプロイします。PC とスマホはブラウザだけです。
ワークスペース作成と Agent 追加には Agent Mail が必要です。hub が落ちていても
既存のチャット吹き出しは読めます。

## 3.0 のインストール

herdr と同じホストで実行します。

| 依存 | 用途 |
| --- | --- |
| [herdr](https://herdr.dev) | agent が入っている端末セッション |
| Git、Python 3.12+、Node.js 20+（npm 含む） | clone、サーバ起動、`web/dist` のビルド |
| [Agent Mail](https://github.com/Dicklesworthstone/mcp_agent_mail) hub（`:8765`） | 身分とチャット配送 |
| ログイン済みの Agent CLI が 1 つ以上 | Codex / Claude / Kimi / OpenCode / Grok / Qoder CLI CN |

clone 先は任意です。`$HOME/github` を作る必要はありません。発見ルートの既定は
リポジトリの親ディレクトリです。親が Home になるときはリポジトリ自身を使います。
別のコード置き場を見るときだけ `COCKPIT_PROJECT_ROOT` を設定してください
（実在するディレクトリ。Home そのものは不可）。

```bash
curl -fsSL https://raw.githubusercontent.com/fyc0451/agent-cockpit/main/install.sh | bash
```

インストーラは `~/agent-cockpit` に clone（既存 checkout ならその場で）し、
venv、Agent Mail、`web/dist` のビルド、`agent-cockpit.service`
（macOS は LaunchAgent）までやります。起動後は
`http://127.0.0.1:8790/#/chat` を開きます。

すでに使える Hub がある場合は再利用し、手作業の
`~/.agent-mail/client.env` は上書きしません。ローカル Hub を飛ばすときは
`AGENT_MAIL_SKIP_HUB=1`。8790 が使用中ならそのプロセスを止めてください。
ポートは変えないでください。起動に失敗したら `./doctor.sh`。

`.venv/bin/python server.py` を直接起動しないでください。
`COCKPIT_NEXT_PROFILE=dev` が無いと 3.0 になりません。サービス unit は
すでに `scripts/dev_server.py` を使います。

systemd なしの手動インストール:

```bash
git clone https://github.com/fyc0451/agent-cockpit.git
cd agent-cockpit
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./install-agent-mail-tools.sh .
./install-agent-mail-hub.sh
npm ci --prefix web
npm run --prefix web build
.venv/bin/python scripts/dev_server.py
# → http://127.0.0.1:8790/#/chat
```

### LAN（任意）

```bash
install -d -m 700 "$HOME/.config/agent-cockpit"
(umask 077; set -o noclobber; openssl rand -hex 32 > \
  "$HOME/.config/agent-cockpit/cockpit.token")
COCKPIT_HOST=0.0.0.0 .venv/bin/python scripts/dev_server.py
```

`http://<LAN-IP>:8790` を開き、`~/.config/agent-cockpit/cockpit.token` を
貼ります。トークンを `.env`、チャット、ログに書かないでください。

> **セキュリティ警告:** リモートアクセスには HTTPS か Tailscale Serve を使って
> ください。平文 HTTP ではログイン cookie が同一ネットワークの第三者に見える
> 可能性があります。Agent Cockpit を直接公衆インターネットに公開しないでください。

### エントリ案内

| 入口 | 実際に入るもの |
| --- | --- |
| `scripts/next_dev.py` / `:18790` | 凍結された Next 2.0 プレビュー。3.0 ではない |
| `./upgrade.sh` | 現在ブランチの upstream を追跡するソース版一括更新（失敗時ロールバック） |
| GitHub Latest の native V2 | ソース 8790 をパッケージ unit に置き換える |

ソース 8790 が動いたあと、インストール先で `./upgrade.sh` を実行します。現在
ブランチの upstream を取得し、依存関係と `web/dist` を再構築し、ソース unit を
再起動して `/health/live` を確認します。旧 V1 Web アップグレード API は引き続き退役
しているため、`COCKPIT_UPGRADE_V2_ENABLED` はオフのままにしてください。

## 初回利用

1. herdr が動いていて、ログイン済みの agent pane が 1 つ以上あることを確認。
2. `http://127.0.0.1:8790/#/chat` を開く。
3. 左でワークスペース / herdr session を選び、右にメンバーが出る。
4. 入力は既定で **排队**。`@` して Enter。空きになってから動く。
   手元を止めるときだけ **打断**。
5. 返信は瀑布流へ。長い過程は「展開過程」に折る。
6. 設定で外観、doctor、ソースアップグレード。

Hash ルート: チャット `/#/chat`、設定 `/#/settings`。不明なパスはチャットへ。
旧ボードはツリー内の `static/index.html` に残っていますが、インストール入口は
もう起動しません。[docs/USER-GUIDE.md](docs/USER-GUIDE.md) を見てください。

## 使い方（旧ボード、install.sh のみ）

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
./upgrade.sh       # fetch・install・build・restart・health gate、失敗時は自動ロールバック
./doctor.sh        # Python、依存、herdr、Agent Mail、認証、サービスをチェック
./uninstall.sh     # user service のみ削除。コード・設定・データは保持
```

upgrader は tracked worktree が clean で、現在ブランチを fast-forward できる場合だけ
動きます。ローカル変更、ahead/diverged commit、同時実行は拒否し、未追跡ファイルは
削除しません。このソース経路は pre-stable 期間向けです。`origin/main` への candidate
公開は引き続き `release_lane.py` を使い、旧 V1 Web アップグレード API は退役のままです。

テストの実行:`.venv/bin/pip install -r requirements-dev.txt` の後に
`.venv/bin/pytest -q`

## プロジェクト構成

```
agent-cockpit/
├── scripts/dev_server.py  3.0 ソース 8790 ランチャー（現在のインストール入口）
├── server.py              互換エントリ（NEXT_PROFILE なしだと旧ボード）
├── source_native_migrate.py / release_lane.py  管理リリースエントリポイント
├── agent_cockpit/         アプリ実装(サーバー、チャット台帳、通信、更新)
├── web/                   3.0 グループチャットフロント（build 先は web/dist）
├── agent_mail_commands/    Agent Mail コマンド実装
├── static/index.html      旧ボード残留（インストール入口は起動しない）
├── tests/                 リグレッション/セキュリティテスト
├── install.sh             3.0 ワンコマンド（web/dist + dev_server）
├── upgrade.sh             ソース版一括更新と自動ロールバック
├── doctor.sh / uninstall.sh
├── agent-cockpit.service  3.0 systemd unit（ExecStart=dev_server.py）
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
