"""インスタのコメントに、公式のプライベート返信（DM）で自動で返すボット。

GitHub Actions（.github/workflows/ig-bot.yml）が定期的に実行する。
公開リポジトリなので、誰にいつ送ったかが読めないよう、記録はハッシュ値にして残す。

必要な環境変数：
  IG_ACCESS_TOKEN  統合ログインのシステムユーザートークン（なければ何もしないで終わる）
  IG_STATE_SALT    記録をハッシュ値にするための秘密の文字列
  IG_CLAIM_URL     すぐ届く仕組み（worker/）の /claim のアドレス（なければ使わない）
  IG_CLAIM_SECRET  その合言葉
"""
import datetime as dt
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "ig_bot_config.json"
LINKS_PATH = ROOT / "docs" / "links" / "links.json"
STATE_PATH = ROOT / "state" / "ig_replied.json"
API = "https://graph.facebook.com"

URL_RE = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|jp|ly|me|co)\b", re.IGNORECASE)
NO_RE = re.compile(r"No\.(\d+)")


class ApiError(Exception):
    pass


def error_text(raw: str) -> str:
    """Meta のエラーから、読むのに必要なところだけを取り出す。"""
    try:
        err = json.loads(raw)["error"]
    except (ValueError, KeyError, TypeError):
        return raw[:300]
    keys = ("code", "error_subcode", "message", "error_user_title", "error_user_msg")
    return " / ".join(str(err[k]) for k in keys if k in err)


def api(method: str, path: str, token: str, params: dict | None = None, body: dict | None = None) -> dict:
    url = f"{API}/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code}: {error_text(e.read().decode(errors='ignore'))}") from e
    except OSError as e:
        raise ApiError(str(e)) from e


def claim(action: str, key: str) -> dict:
    """すぐ届く仕組み（worker/）と送信の記録を分け合う。未設定のときは何もしない。"""
    url, secret = os.environ.get("IG_CLAIM_URL"), os.environ.get("IG_CLAIM_SECRET")
    if not url or not secret:
        return {}
    try:
        req = urllib.request.Request(url.strip(), data=json.dumps({"action": action, "key": key}).encode(), method="POST", headers={
            "Authorization": f"Bearer {secret.strip()}",
            "Content-Type": "application/json",
            "User-Agent": "wo-kix-ig-bot",  # Cloudflare は Python の標準の名前を断る
        })
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ApiError(f"HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        # 合言葉が混ざらないよう、エラーの種類だけを出す
        raise ApiError(type(e).__name__) from e


def fetch_all(path: str, token: str, params: dict) -> list[dict]:
    items, after = [], None
    while True:
        page = api("GET", path, token, {**params, **({"after": after} if after else {})})
        items += page.get("data", [])
        after = page.get("paging", {}).get("cursors", {}).get("after")
        if not after or not page.get("paging", {}).get("next"):
            return items


def parse_time(text: str) -> dt.datetime:
    return dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")


def hash_id(salt: str, *parts: str) -> str:
    return hashlib.sha256("|".join([salt, *parts]).encode()).hexdigest()[:24]


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", text or "").lower()


def is_spam(comment: dict) -> bool:
    text = comment.get("text", "")
    return not text.strip() or bool(URL_RE.search(text))


def commenter(comment: dict) -> str:
    author = comment.get("from") or {}
    return str(author.get("id") or author.get("username") or comment.get("username") or "")


def dm_text(config: dict, no: int) -> str:
    if config["dm_include_url"]:
        guide = f"動画の商品はこちらです👇\n{config['links_page']}#no{no}\n※楽天のページに移動します"
    else:
        guide = f"動画の商品は、プロフィールのリンクの「No.{no}」から見られます。\n※楽天のページに移動します"
    return f"コメントありがとうございます！\n{guide}"


def select_targets(comments: list[dict], me: str, salt: str, media_id: str, state: dict,
                   now: dt.datetime, reply_days: int) -> list[dict]:
    """送る相手を選ぶ。自分・URL入り・連投・期限切れ・送信済みを除く。"""
    deadline = now - dt.timedelta(days=reply_days)
    # 同じ人の同じ文は、2回目以降を連投として除く（古い順に見る）
    seen_texts, seen_people, targets = set(), set(), []
    for c in sorted(comments, key=lambda x: x.get("timestamp", "")):
        who = commenter(c)
        if not who or who == me or c.get("username") == me or (c.get("from") or {}).get("username") == me:
            continue
        text_key = (who, normalize(c.get("text", "")))
        duplicated = text_key in seen_texts
        seen_texts.add(text_key)
        if duplicated or is_spam(c):
            continue
        comment_key = hash_id(salt, "comment", c["id"])
        person_key = hash_id(salt, "person", media_id, who)
        if comment_key in state["comments"] or person_key in state["people"] or person_key in seen_people:
            continue
        if parse_time(c["timestamp"]) < deadline:
            continue
        seen_people.add(person_key)
        targets.append({"comment": c, "comment_key": comment_key, "person_key": person_key})
    return targets


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        return {"comments": state.get("comments", []), "people": state.get("people", [])}
    return {"comments": [], "people": []}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=0) + "\n", encoding="utf-8")


def ensure_subscribed(my_id: str, token: str) -> None:
    """アプリとインスタのアカウントを結ぶ。結ばれていれば何もしない。失敗しても止めない。"""
    try:
        if api("GET", f"{my_id}/subscribed_apps", token).get("data"):
            return
        api("POST", f"{my_id}/subscribed_apps", token, {"subscribed_fields": "comments"})
        print("::notice::アプリとインスタのアカウントを結びました")
    except ApiError as e:
        print(f"::warning::アプリとインスタのアカウントを結べませんでした：{e}")


def run(token: str, salt: str, now: dt.datetime | None = None) -> int:
    now = now or dt.datetime.now(dt.timezone.utc)
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    links = {i["no"]: i for i in json.loads(LINKS_PATH.read_text(encoding="utf-8"))["items"]}
    state = load_state()
    my_id = config["ig_user_id"]
    ensure_subscribed(my_id, token)
    me = api("GET", my_id, token, {"fields": "username"}).get("username", "")

    window = now - dt.timedelta(days=config["reel_window_days"])
    media = fetch_all(f"{my_id}/media", token, {"fields": "id,caption,timestamp,media_product_type", "limit": 50})
    sent, errors = 0, 0
    for m in media:
        if m.get("media_product_type") != "REELS" or parse_time(m["timestamp"]) < window:
            continue
        found = NO_RE.search(m.get("caption") or "")
        if not found or int(found.group(1)) not in links:
            continue
        no = int(found.group(1))
        try:
            comments = fetch_all(f"{m['id']}/comments", token,
                                 {"fields": "id,text,timestamp,username,from", "limit": 50})
        except ApiError as e:
            print(f"::warning::コメントを取得できませんでした（No.{no}）：{e}")
            errors += 1
            continue
        for target in select_targets(comments, me, salt, m["id"], state, now, config["reply_window_days"]):
            if sent >= config["max_dm_per_run"]:
                print(f"1回の上限（{config['max_dm_per_run']}件）に達したので、残りは次回送ります")
                save_state(state)
                return errors
            # すぐ届く仕組み（worker/）が先に送っていれば、ここでは送らない
            try:
                if not claim("claim", target["person_key"]).get("granted", True):
                    continue
            except ApiError as e:
                print(f"::warning::送信の記録を確認できませんでした（No.{no}）：{e}")
                errors += 1
                continue
            try:
                api("POST", f"{my_id}/messages", token, body={
                    "recipient": {"comment_id": target["comment"]["id"]},
                    "message": {"text": dm_text(config, no)},
                })
            except ApiError as e:
                # 止まらずに次へ。記録しないので次回やり直される
                print(f"::warning::送信できませんでした（No.{no}）：{e}")
                errors += 1
                try:
                    claim("release", target["person_key"])
                except ApiError:
                    pass  # 取り消せなくても、10分でやり直せるようになる
                continue
            try:
                claim("done", target["person_key"])
            except ApiError as e:
                print(f"::warning::送信の記録を残せませんでした（No.{no}）：{e}")
            state["comments"].append(target["comment_key"])
            state["people"].append(target["person_key"])
            sent += 1
    save_state(state)
    print(f"送信 {sent} 件／エラー {errors} 件")
    return errors


def main() -> None:
    token, salt = os.environ.get("IG_ACCESS_TOKEN"), os.environ.get("IG_STATE_SALT")
    if not token or not salt:
        print("IG_ACCESS_TOKEN か IG_STATE_SALT が未設定なので、何もしません")
        return
    try:
        run(token, salt)
    except ApiError as e:
        # 次回の定期実行でやり直す
        print(f"::warning::インスタのAPIでエラー：{e}")


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
