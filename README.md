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

投稿文のフックは `scripts/generate.py` の `HOOKS` にある。

## 仕組み

- `scripts/generate.py`：楽天商品検索API（QPS=1、2秒間隔）→ 重複除外（`history.json`）→ 画像を600pxで保存 → テンプレートで投稿文 → `docs/` に確認ページ
- 古い週のフォルダは4週分だけ残して自動削除
- 手元で試す：`python scripts/generate.py --dry-run`
