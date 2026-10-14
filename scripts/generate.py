"""無駄遣い推進課：1週間分の投稿キューを作る。

楽天商品検索APIで商品を集め、画像を保存し、テンプレートで投稿文を作り、
スマホ用の確認ページ docs/index.html を書き出す。

使い方:
  python scripts/generate.py            # 本番（環境変数に楽天のキーが必要）
  python scripts/generate.py --dry-run  # API を呼ばずにダミーデータでページだけ作る
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
HISTORY_PATH = ROOT / "history.json"
JST = dt.timezone(dt.timedelta(hours=9))

API_URL = "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260701"

HOOKS = [
    "これは買わなくていい。でも欲しい。",
    "完全に無駄遣い。なのに気になる。",
    "誰が買うんだ…と思ったら自分だった。",
    "生活に必要か？いいえ。欲しいか？はい。",
    "給料日前に見ちゃいけないやつ。",
    "財布の紐、今日はゆるめでいこう。",
    "レビュー見たら普通に良さそうで困る。",
    "これを部屋に置く人生、ちょっと楽しそう。",
    "無駄だけど、無駄じゃないかもしれない。",
    "ポチる理由はあとから考えればいい。",
    "地味に人に自慢したくなるやつ。",
    "なくても困らない。あったら嬉しい。",
    "今週のご褒美、これでよくない？",
    "見つけてしまったので共有します。",
    "こういうのでいいんだよ、こういうので。",
    "家族に「何それ」って言われたい。",
    "友達へのネタプレゼントにも良さそう。",
    "冷静になる前にカートへ。",
    "無駄遣い推進課、本日のおすすめです。",
    "レビュー数が多いのは、みんな同じ気持ちだから。",
]


# ---------- テキスト整形 ----------

def x_weight(text: str) -> int:
    """X の文字数カウント（ざっくり版）。全角=2、半角=1、URL=23。上限は280。"""
    total = 0
    for part in re.split(r"(https?://\S+)", text):
        if part.startswith("http"):
            total += 23
            continue
        for ch in part:
            total += 1 if ord(ch) < 0x1100 or unicodedata.east_asian_width(ch) in ("Na", "H") else 2
    return total


def clean_name(name: str, limit: int = 32) -> str:
    """楽天の長い商品名から、飾りや宣伝文句を落として短くする。"""
    s = unicodedata.normalize("NFKC", name)
    s = re.sub(r"[【\[［(（<＜《〈≪«].*?[】\]］)）>＞》〉≫»]", " ", s)
    s = re.sub(r"^[^【]*?】", " ", s)  # 閉じだけ残った【】
    s = re.sub(r"[\\＼].*?[/／]", " ", s)  # ＼キャッチコピー／
    s = re.sub(r"[★☆◆◇■□●○♪※!！]+", " ", s)
    s = re.sub(r"(送料無料|ポイント\d+倍|楽天\S*位|あす楽|即納|クーポン\S*|最大\S*OFF|\d+%OFF|レビュー(で|を書いて|特典)\S*|P\d+倍\S*|\d+日\d+時\S*|\d+時[〜~]\S*|を\d+回|新色登場|\d+冠|全店\S*価格\S*|\d+/\d+\s*\d+:\d+\S*|安全検査実施済)", " ", s)
    s = s.strip(" 「」『』")
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= limit:
        return s
    cut = s[:limit]
    space = cut.rfind(" ")
    return (cut[:space] if space >= limit // 2 else cut).rstrip(" ・/、,") + "…"


COLOR_WORDS = {
    "ブラック", "ホワイト", "グレー", "グレイ", "ネイビー", "ベージュ", "ブラウン", "レッド", "ピンク",
    "ブルー", "グリーン", "イエロー", "オレンジ", "パープル", "シルバー", "ゴールド", "黒", "白",
    "全色", "単品", "新色", "カラー", "サイズ", "大", "中", "小", "S", "M", "L",
}


def name_tokens(name: str) -> set[str]:
    """サイズ・数量・型番を除いた、商品名の中身の単語。"""
    words = re.split(r"[\s・/|｜,、]+", clean_name(name, 80).rstrip("…"))
    out = set()
    for w in words:
        w = re.sub(r"\d+(\.\d+)?(cm|mm|m|ml|l|g|kg|個|枚|本|点|袋|箱|入|セット|L|W|mAh|インチ)?", "", w, flags=re.I)
        w = re.sub(r"[〜~×xX\-]+", "", w)
        if w in COLOR_WORDS:
            continue
        if len(w) >= 2:
            out.add(w.lower())
    return out


def is_similar(a: dict, b: dict) -> bool:
    """サイズ違い・色違い・セット数違いなど、ほぼ同じ商品かどうか。"""
    if a.get("imageUrl") and a.get("imageUrl", "").split("?")[0] == b.get("imageUrl", "").split("?")[0]:
        return True
    ta, tb = name_tokens(a["itemName"]), name_tokens(b["itemName"])
    if not ta or not tb:
        return False
    jac = len(ta & tb) / len(ta | tb)
    same_shop = a.get("shopName") and a.get("shopName") == b.get("shopName")
    return jac >= (0.45 if same_shop else 0.7)


def build_post(item: dict, rng: random.Random, hook: str | None = None) -> str:
    name = clean_name(item["itemName"])
    price = f"{int(item['itemPrice']):,}円"
    review = ""
    if item.get("reviewCount"):
        review = f"{float(item['reviewAverage']):.1f}（{int(item['reviewCount']):,}件）"
    tags = " ".join(CONFIG["hashtags"])
    link = item["affiliateUrl"] or item["itemUrl"]

    hook = hook or rng.choice(HOOKS)
    lines = [hook, "", f"▶ {name}", f"💰 {price}"]
    if review:
        lines.append(f"⭐ {review}")
    lines += ["", f"{link}", tags]
    text = "\n".join(lines)

    # 長すぎたら商品名を詰める
    shrink = 32
    while x_weight(text) > 270 and shrink > 12:
        shrink -= 4
        lines[2] = f"▶ {clean_name(item['itemName'], shrink)}"
        text = "\n".join(lines)
    return text


# ---------- 一言（Gemini） ----------

GEMINI_PROMPT = """あなたはXの匿名アカウント「無駄遣い推進課」の中の人です。
楽天の商品を紹介する投稿の、冒頭の「一言」を書いてください。

【キャラ・口調】
- リアクション系YouTuberみたいなノリの、砕けたタメ口。商品を見た瞬間の素のツッコミや心の声
- 「です・ます」「〜ですね」「〜ですよ」は禁止
- 使っていい言い回しの例：「〜じゃん」「〜すぎん？」「いや〜」「普通に〜」「〜なんよ」「〜でしょ」「ずるくない？」「これは〜」
- 1文目でツッコミかリアクション → 2文目で「でも欲しい」理由、の流れが基本（1文で完結してもOK）
- 「（笑）」「w」は全体で3回まで
- 同じ口癖に頼らない。「すぎ」「すぎん？」は全体で6回まで、「なんよ」「ずるい」「最高」「神」はそれぞれ全体で3回まで

【お手本（この雰囲気で。言い回しはコピーしない）】
- 光る耳かき →「耳かきが光る必要ある？って思ったけど、奥まで見えたら絶対ハマるやつじゃん」
- 業務用チョコ150粒 →「150粒は多すぎん？…いや、3日でなくなる未来しか見えない」
- やる気のない猫のTシャツ →「この猫、完全に日曜の夕方の自分。着るしかないでしょ」
- バナナスタンド →「バナナ吊るすだけで台所がちょっとオシャレになるの、ずるくない？」

【ルール】
- 1件につき1つ、全角15〜45文字
- その商品ならではの具体的なポイント（使う場面・見た目・量・意外性・値段感など）に必ず触れる。どの商品にも使える汎用フレーズは禁止
- 商品名をそのまま繰り返さない
- 効果・効能は書かない。特に美容・健康・マッサージ・寝具・家電は「疲れが取れる」「痩せる」「毛穴」「ストレスが減る」「よく眠れる」「ストレス解消」「罪悪感なく」「ヘルシー」など体や心への効果に触れず、見た目・使う場面・感想にとどめる
- 「最安」「No.1」「絶対〜できる」などの誇大表現は禁止
- 絵文字・ハッシュタグ・URLは入れない
- 書き出しや語尾が他の件と被らないようにする

【出力】
JSON配列のみ。入力と同じ順番で {"i": 番号, "hook": "一言"} を並べる。

【商品】
"""


def gemini_json(prompt: str, label: str, temperature: float = 1.0):
    """Gemini に JSON を返させる。全モデルで失敗したら None。"""
    key = os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json", "temperature": temperature},
    }
    for model in CONFIG.get("gemini_models") or ["gemini-flash-latest"]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=240) as r:
                    data = json.loads(r.read().decode("utf-8"))
                text = data["candidates"][0]["content"]["parts"][0]["text"]
                text = re.sub(r"^```(json)?|```$", "", text.strip()).strip()
                result = json.loads(text)
                print(f"::notice::Gemini（{model}）：{label} OK")
                return result
            except urllib.error.HTTPError as e:
                raw = e.read().decode("utf-8", "replace")
                try:
                    msg = json.loads(raw)["error"]["message"]
                except Exception:  # noqa: BLE001
                    msg = raw
                msg = re.sub(r"\s+", " ", msg)[:300]
                print(f"::warning::Gemini（{model}）{label} エラー {e.code}: {msg}")
                if e.code in (429, 500, 503) and attempt < 2:
                    time.sleep(20)
                    continue
                break  # 403/404 などは次のモデルへ
            except Exception as e:  # noqa: BLE001
                print(f"::warning::Gemini（{model}）{label} の結果を読めませんでした: {e}")
                if attempt < 2:
                    time.sleep(5)
                    continue
                break
    return None


def gemini_hooks(items: list[dict]) -> dict[int, str]:
    if not items:
        return {}
    lines = []
    for i, it in enumerate(items):
        desc = " / ".join(x for x in [it.get("catchcopy", ""), it.get("caption", "")[:150]] if x)
        lines.append(f'{i}. {clean_name(it["itemName"], 60)}｜{int(it["itemPrice"]):,}円｜{desc}')
    rows = gemini_json(GEMINI_PROMPT + "\n".join(lines), "一言")
    out: dict[int, str] = {}
    for row in rows or []:
        try:
            h = str(row.get("hook", "")).strip()
            h = re.sub(r"https?://\S+|#\S+", "", h).strip()
            if 4 <= len(h) <= 60:
                out[int(row["i"])] = h
        except Exception:  # noqa: BLE001
            continue
    if not out:
        print("::warning::一言はテンプレートで代用しました")
    else:
        print(f"一言：{len(out)}/{len(items)} 件")
    return out


# ---------- ジャンル ----------

GENRES = {
    "ポーチ・バッグ": ["ポーチ", "バッグ", "ポシェット", "ショルダー", "リュック", "財布", "パスケース"],
    "充電・ケーブル": ["充電", "ケーブル", "バッテリー", "アダプター", "電源タップ", "usb"],
    "ガジェット": ["ガジェット", "ラジオ", "スピーカー", "タブレット", "イヤホン", "カードリーダー"],
    "生活家電": ["クリーナー", "掃除機", "加湿", "扇風機", "ヒーター", "ドライヤー"],
    "照明・時計": ["ライト", "ランプ", "照明", "時計", "プラネタリウム"],
    "収納・整理": ["収納", "マグネット", "フック", "ラック", "ボックス", "ホルダー", "棚"],
    "キッチン": ["キッチン", "まな板", "ボウル", "ザル", "トング", "計量", "水切り", "調味料", "包丁", "ナイフ"],
    "掃除・日用品": ["掃除", "ゴミ", "洗剤", "スポンジ", "クロス", "ラップ", "ティッシュ"],
    "バス・トイレ": ["浴室", "お風呂", "バス", "トイレ", "石鹸", "シャワー"],
    "お菓子": ["お菓子", "チョコ", "クッキー", "けんぴ", "おかき", "マカロン", "羊羹", "スイーツ"],
    "食品・グルメ": ["ハンバーグ", "肉", "米", "ラーメン", "カレー", "食品", "グルメ"],
    "ファッション": ["tシャツ", "パンツ", "帽子", "ベルト", "靴", "フェイスカバー", "ネックレス"],
    "インテリア・雑貨": ["インテリア", "置物", "カーテン", "風鈴", "雑貨", "トレー"],
    "リラックス・寝具": ["枕", "アロマ", "アイマスク", "ホットマット", "湯たんぽ", "毛布"],
    "美容・ケア": ["マッサージ", "ヘッドスパ", "ems", "温熱", "耳かき", "爪", "美容"],
    "アウトドア・旅行": ["キャンプ", "アウトドア", "旅行", "トラベル", "スーツケース", "圧縮"],
    "車・自転車": ["車", "カー用品", "自転車", "バイク"],
    "文房具・デスク": ["ペン", "文房具", "デスク", "ファイル", "ノート"],
    "ペット": ["犬", "猫用", "ペット"],
    "ギフト・名入れ": ["名入れ", "ギフト", "プレゼント", "還暦", "出産祝い", "はんこ", "印鑑"],
    "おもしろ・ネタ": ["おもしろ", "ネタ", "面白", "ジョーク", "パーティー"],
}
GENRE_NAMES = list(GENRES) + ["その他"]


def keyword_genre(it: dict) -> str:
    name = clean_name(it["itemName"], 80).lower()
    for g, words in GENRES.items():
        if any(w in name for w in words):
            return g
    return "その他"


def classify(items: list[dict]) -> list[str]:
    """商品ごとのジャンル。Gemini で判定し、だめならキーワードで判定。"""
    prompt = (
        "次の楽天の商品それぞれに、ジャンルを1つ選んでください。\n"
        f"ジャンルの候補（この中から必ず選ぶ）：{'、'.join(GENRE_NAMES)}\n"
        "見た目より『何に使う物か』で選ぶ。例：スマホを入れるポーチは「ポーチ・バッグ」、"
        "名入れでもTシャツなら「ファッション」。\n"
        'JSON配列のみで、入力と同じ順番に {"i": 番号, "genre": "ジャンル"} を並べる。\n\n'
        + "\n".join(f"{i}. {clean_name(it['itemName'], 60)}" for i, it in enumerate(items))
    )
    result = [keyword_genre(it) for it in items]
    rows = gemini_json(prompt, "ジャンル判定", temperature=0.2)
    hit = 0
    for row in rows or []:
        try:
            i, g = int(row["i"]), str(row["genre"]).strip()
            if 0 <= i < len(items) and g in GENRE_NAMES:
                result[i] = g
                hit += 1
        except Exception:  # noqa: BLE001
            continue
    print(f"ジャンル判定：AI {hit}件 / キーワード {len(items) - hit}件")
    return result


def pick_by_genre(cands: list[dict], genres: list[str], need: int, cap: int) -> list[dict]:
    """ジャンルごとの上限を守って選ぶ。足りなければ上限を少しずつ緩める。"""
    for limit in range(cap, cap + 4):
        counts: dict[str, int] = {}
        picked = []
        for it, g in zip(cands, genres):
            if len(picked) >= need:
                break
            if counts.get(g, 0) >= limit:
                continue
            counts[g] = counts.get(g, 0) + 1
            it["genre"] = g
            picked.append(it)
        if len(picked) >= need:
            if limit > cap:
                print(f"::warning::候補のジャンルが偏っていたため、上限を{limit}件に緩めました")
            break
    # 同じジャンルが続かないように並べ替え
    ordered: list[dict] = []
    rest = picked[:]
    while rest:
        prev = ordered[-1]["genre"] if ordered else None
        idx = next((k for k, x in enumerate(rest) if x["genre"] != prev), 0)
        ordered.append(rest.pop(idx))
    summary = {}
    for x in ordered:
        summary[x["genre"]] = summary.get(x["genre"], 0) + 1
    print("::notice::ジャンル内訳：" + "、".join(f"{k}{v}" for k, v in sorted(summary.items(), key=lambda kv: -kv[1])))
    return ordered


# ---------- 楽天 API ----------

def api_search(keyword: str, page: int = 1) -> list[dict]:
    params = {
        "applicationId": os.environ["RAKUTEN_APP_ID"],
        "format": "json",
        "formatVersion": "2",
        "keyword": keyword,
        "hits": "30",
        "page": str(page),
        "sort": "-reviewCount",
        "minPrice": str(CONFIG["min_price"]),
        "maxPrice": str(CONFIG["max_price"]),
        "imageFlag": "1",
        "availability": "1",
    }
    if os.environ.get("RAKUTEN_ACCESS_KEY"):
        params["accessKey"] = os.environ["RAKUTEN_ACCESS_KEY"]
    if os.environ.get("RAKUTEN_AFFILIATE_ID"):
        params["affiliateId"] = os.environ["RAKUTEN_AFFILIATE_ID"]

    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    headers = {"User-Agent": "wo-kix-queue/1.0"}
    origin = os.environ.get("RAKUTEN_ORIGIN")
    if origin:
        headers["Origin"] = origin
        headers["Referer"] = origin.rstrip("/") + "/"

    for attempt in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
            return data.get("Items", [])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code == 429 and attempt < 2:
                time.sleep(5)
                continue
            raise SystemExit(f"楽天APIエラー {e.code}: {body}")
        finally:
            time.sleep(2)  # QPS=1 を厳守（余裕を見て2秒）
    return []


def normalize(raw: dict) -> dict:
    images = raw.get("mediumImageUrls") or []
    img = images[0] if images else ""
    if isinstance(img, dict):
        img = img.get("imageUrl", "")
    img = re.sub(r"_ex=\d+x\d+", "_ex=600x600", img)
    if img and "_ex=" not in img:
        img += ("&" if "?" in img else "?") + "_ex=600x600"
    return {
        "itemCode": raw.get("itemCode", ""),
        "itemName": raw.get("itemName", ""),
        "itemPrice": raw.get("itemPrice", 0),
        "itemUrl": raw.get("itemUrl", ""),
        "affiliateUrl": raw.get("affiliateUrl", ""),
        "imageUrl": img,
        "reviewAverage": raw.get("reviewAverage", 0),
        "reviewCount": raw.get("reviewCount", 0),
        "shopName": raw.get("shopName", ""),
        "catchcopy": raw.get("catchcopy", ""),
        "caption": re.sub(r"<[^>]+>|\s+", " ", raw.get("itemCaption", ""))[:300],
    }


def collect(need: int, history: set[str], rng: random.Random, per_keyword: int = 3) -> list[dict]:
    keywords = CONFIG["keywords"][:]
    rng.shuffle(keywords)
    picked: list[dict] = []
    seen_codes = set(history)
    seen_shops: dict[str, int] = {}

    page = 1
    while len(picked) < need and page <= 3:
        for kw in keywords:
            if len(picked) >= need:
                break
            items = [normalize(x) for x in api_search(kw, page)]
            rng.shuffle(items)
            took = 0
            for it in items:
                if took >= per_keyword or len(picked) >= need:
                    break
                if not it["imageUrl"] or it["itemCode"] in seen_codes:
                    continue
                if it["reviewCount"] < CONFIG["min_review_count"]:
                    continue
                if float(it["reviewAverage"] or 0) < CONFIG["min_review_average"]:
                    continue
                if seen_shops.get(it["shopName"], 0) >= 2:
                    continue
                if any(is_similar(it, q) for q in picked):
                    print(f"  類似商品を除外: {clean_name(it['itemName'], 30)}")
                    continue  # サイズ違い・色違いなど
                it["keyword"] = kw
                picked.append(it)
                seen_codes.add(it["itemCode"])
                seen_shops[it["shopName"]] = seen_shops.get(it["shopName"], 0) + 1
                took += 1
        page += 1
    return picked


def download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "wo-kix-queue/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
        return dest.stat().st_size > 1000
    except Exception as e:  # noqa: BLE001
        print(f"  画像の取得に失敗: {e}", file=sys.stderr)
        return False


# ---------- ダミーデータ ----------

def dummy_items(n: int) -> list[dict]:
    names = [
        "【送料無料】光る ネコ型 デスクライト USB充電 かわいい 間接照明 ギフト プレゼント",
        "★楽天1位★ 自動でかき混ぜる マグカップ ステンレス 保温 コーヒー 紅茶",
        "巨大 ふわふわ 食パン クッション 抱き枕 70cm おもしろ雑貨",
        "【ポイント10倍】折りたたみ ミニ焚き火台 ソロキャンプ 軽量 収納袋付き",
        "業務用 うまい棒 600本 まとめ買い 駄菓子 詰め合わせ",
    ]
    out = []
    for i in range(n):
        out.append({
            "itemCode": f"dummy:{i}",
            "itemName": names[i % len(names)],
            "itemPrice": 980 + i * 370,
            "itemUrl": f"https://item.rakuten.co.jp/dummy/{i}/",
            "affiliateUrl": f"https://hb.afl.rakuten.co.jp/hgc/dummy{i}/",
            "imageUrl": "",
            "reviewAverage": 4.0 + (i % 9) / 10,
            "reviewCount": 25 + i * 13,
            "shopName": f"ダミーショップ{i % 6}",
            "keyword": "ダミー",
        })
    return out


def dummy_image(dest: Path, label: str) -> None:
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="600" height="600">'
        '<rect width="600" height="600" fill="#e8e2d6"/>'
        f'<text x="300" y="315" font-size="48" text-anchor="middle" fill="#6b5f4b">{html.escape(label)}</text>'
        "</svg>"
    )
    dest.write_text(svg, encoding="utf-8")


# ---------- 確認ページ ----------

def render_page(week_key: str, week_label: str, posts: list[dict], generated: str) -> str:
    cards = []
    current_day = None
    for p in posts:
        if p["date"] != current_day:
            current_day = p["date"]
            cards.append(f'<h2 class="day">{html.escape(p["dateLabel"])}</h2>')
        text_attr = html.escape(p["text"], quote=True)
        cards.append(f"""
<article class="card" data-id="{html.escape(p['id'])}">
  <div class="meta"><span class="slot">{html.escape(p['slot'])}</span><span class="kw">{html.escape(p.get('keyword', ''))}</span></div>
  <img src="{html.escape(p['image'])}" alt="" loading="lazy" data-file="{html.escape(p['fileName'])}">
  <pre class="text">{html.escape(p['text'])}</pre>
  <div class="actions">
    <button class="go" data-text="{text_attr}">保存＋コピー</button>
    <label class="done"><input type="checkbox"> 済</label>
  </div>
  <a class="src" href="{html.escape(p['itemUrl'])}" target="_blank" rel="noopener">商品ページを確認</a>
</article>""")

    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex, nofollow">
<title>投稿キュー {html.escape(week_label)}</title>
<style>
:root {{ --bg:#f6f3ee; --card:#fff; --ink:#222; --sub:#857a68; --line:#e4ddd1; --accent:#c4512c; --ok:#3f8a5a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16140f; --card:#221f19; --ink:#eee8dc; --sub:#a39783; --line:#3a352c; --accent:#e2703f; --ok:#5fb27c; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--ink); font:16px/1.6 -apple-system, "Hiragino Sans", sans-serif; padding:0 16px 80px; }}
header {{ position:sticky; top:0; background:var(--bg); padding:14px 0 10px; z-index:2; border-bottom:1px solid var(--line); }}
header h1 {{ margin:0; font-size:18px; }}
header p {{ margin:2px 0 0; color:var(--sub); font-size:13px; }}
.progress {{ height:6px; background:var(--line); border-radius:3px; margin-top:8px; overflow:hidden; }}
.progress i {{ display:block; height:100%; background:var(--ok); width:0; transition:width .3s; }}
.day {{ font-size:15px; margin:26px 0 8px; color:var(--sub); }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:14px; padding:12px; margin-bottom:14px; }}
.card.is-done {{ opacity:.45; }}
.meta {{ display:flex; gap:8px; font-size:13px; color:var(--sub); margin-bottom:8px; }}
.slot {{ font-weight:700; color:var(--accent); }}
.card img {{ width:100%; height:260px; object-fit:contain; background:#fff; border-radius:10px; }}
.text {{ white-space:pre-wrap; word-break:break-all; font:inherit; font-size:14px; margin:10px 0; }}
.actions {{ display:flex; gap:10px; align-items:center; }}
.go {{ flex:1; font:inherit; font-weight:700; padding:13px; border:0; border-radius:10px; background:var(--accent); color:#fff; }}
.go.copied {{ background:var(--ok); }}
.done {{ display:flex; align-items:center; gap:6px; padding:10px 6px; font-size:15px; }}
.done input {{ width:22px; height:22px; }}
.src {{ display:inline-block; margin-top:8px; font-size:13px; color:var(--sub); }}
.hide-done .card.is-done {{ display:none; }}
.toggle {{ font:inherit; font-size:13px; margin-top:8px; background:none; border:1px solid var(--line); color:var(--ink); border-radius:8px; padding:4px 10px; }}
</style>
</head>
<body>
<header>
  <h1>投稿キュー {html.escape(week_label)}</h1>
  <p>{len(posts)}件 ・ 生成 {html.escape(generated)} ・ <span id="count">0</span>件 済</p>
  <div class="progress"><i id="bar"></i></div>
  <button class="toggle" id="toggle">済を隠す</button>
</header>
<main>
{''.join(cards)}
</main>
<script>
(function () {{
  var KEY = 'wokix-done-{week_key}';
  var done = {{}};
  try {{ done = JSON.parse(localStorage.getItem(KEY) || '{{}}'); }} catch (e) {{}}
  function save() {{ try {{ localStorage.setItem(KEY, JSON.stringify(done)); }} catch (e) {{}} }}
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  function refresh() {{
    var n = 0;
    cards.forEach(function (c) {{ var d = !!done[c.dataset.id]; c.classList.toggle('is-done', d); c.querySelector('.done input').checked = d; if (d) n++; }});
    document.getElementById('count').textContent = n;
    document.getElementById('bar').style.width = (cards.length ? n / cards.length * 100 : 0) + '%';
  }}

  // 共有シートを素早く出せるよう、画像は先に読み込んでおく
  var files = {{}};
  cards.forEach(function (c) {{
    var img = c.querySelector('img');
    fetch(img.getAttribute('src')).then(function (r) {{ return r.blob(); }}).then(function (b) {{
      files[c.dataset.id] = new File([b], img.dataset.file, {{ type: b.type || 'image/jpeg' }});
    }}).catch(function () {{}});
  }});

  cards.forEach(function (c) {{
    var btn = c.querySelector('.go');
    btn.addEventListener('click', function () {{
      var text = btn.dataset.text;
      var copied = false;
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(text).then(function () {{ copied = true; }}).catch(function () {{}});
      }}
      var file = files[c.dataset.id];
      if (file && navigator.canShare && navigator.canShare({{ files: [file] }})) {{
        navigator.share({{ files: [file] }}).catch(function () {{}});
      }} else {{
        window.open(c.querySelector('img').src, '_blank');
      }}
      btn.textContent = 'コピー済み ✓';
      btn.classList.add('copied');
    }});
    c.querySelector('.done input').addEventListener('change', function (e) {{
      done[c.dataset.id] = e.target.checked; save(); refresh();
    }});
  }});

  document.getElementById('toggle').addEventListener('click', function () {{
    var on = document.body.classList.toggle('hide-done');
    this.textContent = on ? '済も表示' : '済を隠す';
  }});
  refresh();
}})();
</script>
</body>
</html>
"""


# ---------- メイン ----------

def next_monday(today: dt.date) -> dt.date:
    days = (7 - today.weekday()) % 7 or 7
    return today + dt.timedelta(days=days)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="API を呼ばずダミーで生成")
    ap.add_argument("--start", help="開始日 YYYY-MM-DD（省略時は次の月曜）")
    args = ap.parse_args()

    now = dt.datetime.now(JST)
    start = dt.date.fromisoformat(args.start) if args.start else next_monday(now.date())
    week = start.strftime("%Y%m%d")
    slots = CONFIG["slots"]
    need = CONFIG["days"] * len(slots)
    rng = random.Random(week)

    history = json.loads(HISTORY_PATH.read_text(encoding="utf-8")) if HISTORY_PATH.exists() else []
    week_dates = {(start + dt.timedelta(days=d)).isoformat() for d in range(CONFIG["days"])}
    history = [h for h in history if h.get("date") not in week_dates]  # 作り直し時は前回分を外す
    hist_codes = {h["itemCode"] for h in history}

    if args.dry_run:
        items = dummy_items(need)
    else:
        if not os.environ.get("RAKUTEN_APP_ID"):
            raise SystemExit("RAKUTEN_APP_ID が設定されていません（GitHub の Secrets を確認）")
        pool = collect(need * 3, hist_codes, rng, per_keyword=8)
        print(f"::notice::候補 {len(pool)} 件を取得")
        genres = classify(pool)
        items = pick_by_genre(pool, genres, need, CONFIG.get("genre_cap", 2))
        print(f"{len(items)}/{need} 件を選択")

    week_dir = DOCS / week
    if week_dir.exists():
        shutil.rmtree(week_dir)
    week_dir.mkdir(parents=True)

    hooks = {} if args.dry_run else gemini_hooks(items)

    posts = []
    youbi = "月火水木金土日"
    for i, it in enumerate(items):
        day = start + dt.timedelta(days=i // len(slots))
        slot = slots[i % len(slots)]
        base = f"{day:%m%d}_{i % len(slots) + 1}"
        if args.dry_run:
            fname = f"{base}.svg"
            dummy_image(week_dir / fname, base)
        else:
            fname = f"{base}.jpg"
            if not download(it["imageUrl"], week_dir / fname):
                continue
        text = build_post(it, rng, hooks.get(i))
        (week_dir / f"{base}.txt").write_text(text, encoding="utf-8")
        posts.append({
            "id": base,
            "date": day.isoformat(),
            "dateLabel": f"{day.month}/{day.day}（{youbi[day.weekday()]}）",
            "slot": slot,
            "image": f"{week}/{fname}",
            "fileName": f"wokix_{base}{Path(fname).suffix}",
            "text": text,
            "itemUrl": it["itemUrl"],
            "keyword": it.get("genre") or it.get("keyword", ""),
            "itemCode": it["itemCode"],
        })

    label = f"{start.month}/{start.day}週"
    page = render_page(week, label, posts, now.strftime("%m/%d %H:%M"))
    (week_dir / "index.html").write_text(page.replace(f'src="{week}/', 'src="'), encoding="utf-8")
    (DOCS / "index.html").write_text(page, encoding="utf-8")
    (week_dir / "posts.json").write_text(json.dumps(posts, ensure_ascii=False, indent=1), encoding="utf-8")

    if not args.dry_run:
        history += [{"itemCode": p["itemCode"], "date": p["date"]} for p in posts]
        HISTORY_PATH.write_text(json.dumps(history[-2000:], ensure_ascii=False, indent=0), encoding="utf-8")

        # 古い週を削除
        weeks = sorted(d for d in DOCS.iterdir() if d.is_dir() and re.fullmatch(r"\d{8}", d.name))
        for old in weeks[: -CONFIG["keep_weeks"]]:
            shutil.rmtree(old)

    print(f"{label}：{len(posts)}件の投稿を docs/ に書き出しました")


if __name__ == "__main__":
    try:
        main()
    except SystemExit as e:
        if e.code not in (None, 0):
            # GitHub Actions の画面にエラー内容を表示する
            print(f"::error::{e.code}")
        raise
    except Exception as e:  # noqa: BLE001
        print(f"::error::{type(e).__name__}: {e}")
        raise
