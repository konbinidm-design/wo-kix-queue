# wo-kix-queue

無駄遣い推進課（@wo_kix）の投稿キュー。毎週日曜 7:00（JST）に GitHub Actions が楽天から1週間分（7日×5枠＝35件）の商品を集め、画像と投稿文をそろえて確認ページを更新する。

スマホで確認ページを開き、「保存＋コピー」→ SocialDog に貼り付けて予約するだけ。

## 初回セットアップ（スマホのブラウザで、PC版サイト表示にして操作）

1. **リポジトリを Public にする**
   Settings → General → 一番下の Danger Zone → Change visibility → Public
   （無料プランでは Public でないと GitHub Pages が使えない。キーは Secrets に入れるので公開されない）
2. **Secrets を登録**
   Settings → Secrets and variables → Actions → New repository secret

   | 名前 | 中身 |
   |---|---|
   | `RAKUTEN_APP_ID` | 楽天のアプリID |
   | `RAKUTEN_ACCESS_KEY` | 楽天のアクセスキー |
   | `RAKUTEN_AFFILIATE_ID` | 楽天アフィリエイトID |
   | `RAKUTEN_ORIGIN` | 楽天アプリに登録した許可サイトのURL（今まで Origin ヘッダーに入れていた値） |
   | `GEMINI_API_KEY` | Google AI Studio のAPIキー（商品ごとの一言を生成。なければテンプレートで代用） |

3. **Pages を有効化**
   Settings → Pages → Source: Deploy from a branch → Branch: `main` / `/docs` → Save
4. **テスト実行**
   Actions → 週次投稿キュー生成 → Run workflow（まずは「ダミーデータで試す」にチェック → 次はチェックなしで本番）
5. 数分後に `https://konbinidm-design.github.io/wo-kix-queue/` を開いて、ホーム画面に追加

## 調整したいとき

`config.json` を編集する。

- `keywords`：商品を探すキーワード
- `slots`：投稿時刻（1日の枠数もこれで決まる）
- `min_price` / `max_price`：価格帯
- `min_review_count` / `min_review_average`：レビューの足切り
- `hashtags`：投稿の末尾につくタグ（ステマ規制対策で `#PR` は残す）

商品ごとの一言の指示（キャラ・ルール）は `scripts/generate.py` の `GEMINI_PROMPT`、予備のテンプレートは `HOOKS` にある。

## 仕組み

- `scripts/generate.py`：楽天商品検索API（QPS=1、2秒間隔）→ 重複除外（`history.json`）→ 画像を600pxで保存 → テンプレートで投稿文 → `docs/` に確認ページ
- 古い週のフォルダは4週分だけ残して自動削除
- 手元で試す：`python scripts/generate.py --dry-run`

## 商品リンクページとインスタの自動返信

- `docs/links/`：インスタのプロフィールに貼る商品リンクページ（https://konbinidm-design.github.io/wo-kix-queue/links/ ）。中身は `docs/links/links.json`。もしも動画の `pipeline/publish_link.py` が追加する。
- `scripts/ig_bot.py`（5分おき）：インスタのコメントにプライベート返信を送る。送信済みの記録は `state/ig_replied.json` にハッシュ値で残す。
- `ig_bot_config.json` の `dm_include_url`：DMに商品ページのURLを入れるか。今は `true`（2026-10-31 ユーザーの判断）。楽天アフィリエイトのガイドラインは、自動ツールでアフィリエイトリンクやそれを載せたページのURLをSNSのDMに載せることを禁止している点に注意。
- `worker/`（Cloudflare Workers）：コメントの連絡（Webhook）を受けて、すぐ返信する。送信の記録はここに1つにまとめ、5分おきのボットは予備として同じ記録を見る。Secrets の `IG_CLAIM_URL`、`IG_CLAIM_SECRET` が未設定の間、ボットは今までどおり単独で動く。
- Instagram API with Unified Login（graph.facebook.com）を使う。トークンはシステムユーザートークンで、Meta の画面では無期限と表示されるので、更新の仕組みは置いていない。
- 必要な Secrets：`IG_ACCESS_TOKEN`、`IG_STATE_SALT`。未設定の間は何もしない。
