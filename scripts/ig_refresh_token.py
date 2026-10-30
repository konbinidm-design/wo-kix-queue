"""インスタの長期アクセストークン（60日で切れる）を更新し、GitHub の Secret を書き換える。

失敗したら GitHub の Issue を作って知らせる（.github/workflows/ig-token.yml から実行）。

必要な環境変数：
  IG_ACCESS_TOKEN    今のトークン
  SECRETS_PAT        このリポジトリの Secrets を書き換えられる細かい権限付きトークン
  GITHUB_TOKEN       Issue を作るためのトークン（Actions が自動で用意する）
  GITHUB_REPOSITORY  owner/name（Actions が自動で用意する）
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

from nacl import encoding, public


def github(method: str, path: str, token: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"https://api.github.com/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def refresh(token: str) -> str:
    query = urllib.parse.urlencode({"grant_type": "ig_refresh_token", "access_token": token})
    with urllib.request.urlopen(f"https://graph.instagram.com/refresh_access_token?{query}", timeout=30) as r:
        return json.loads(r.read().decode())["access_token"]


def save_secret(repo: str, pat: str, name: str, value: str) -> None:
    key = github("GET", f"repos/{repo}/actions/secrets/public-key", pat)
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    encrypted = base64.b64encode(box.encrypt(value.encode())).decode()
    github("PUT", f"repos/{repo}/actions/secrets/{name}", pat, {"encrypted_value": encrypted, "key_id": key["key_id"]})


def report_failure(repo: str, token: str, reason: str) -> None:
    title = "インスタのトークン更新に失敗しました"
    github("POST", f"repos/{repo}/issues", token, {
        "title": title,
        "body": f"コメントへの自動返信が止まる前に確認が必要です（トークンは60日で切れます）。\n\n```\n{reason}\n```",
    })


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    token = os.environ.get("IG_ACCESS_TOKEN")
    if not token:
        print("IG_ACCESS_TOKEN が未設定なので、何もしません")
        return
    try:
        save_secret(repo, os.environ["SECRETS_PAT"], "IG_ACCESS_TOKEN", refresh(token))
        print("トークンを更新しました")
    except (KeyError, OSError, ValueError) as e:
        # 例外の内容にトークンが入らないよう、種類とHTTPの状態だけを残す
        reason = f"{type(e).__name__}: {getattr(e, 'code', '')}"
        report_failure(repo, os.environ["GITHUB_TOKEN"], reason)
        sys.exit(reason)


if __name__ == "__main__":
    main()
