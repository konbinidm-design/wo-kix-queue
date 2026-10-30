// インスタのコメントの連絡（Webhook）を受けて、すぐプライベート返信（DM）を送る。
// 5分おきのボット（scripts/ig_bot.py）は予備として残し、送信の記録はここの Claims に1つにまとめる。
//
// 必要な秘密の値（wrangler secret put で入れる。ファイルには書かない）：
//   IG_ACCESS_TOKEN  システムユーザートークン（ボットと同じ）
//   IG_STATE_SALT    記録をハッシュ値にする文字列（ボットと同じ）
//   IG_APP_SECRET    Meta アプリのシークレット（連絡が本物か確かめる）
//   VERIFY_TOKEN     Meta に登録する確認用の文字列
//   CLAIM_SECRET     ボットが /claim を呼ぶときの合言葉
import { DurableObject } from "cloudflare:workers";

const API = "https://graph.facebook.com";
const URL_RE = /https?:\/\/|www\.|\b[\w-]+\.(?:com|net|org|jp|ly|me|co)\b/i;
const NO_RE = /No\.(\d+)/;
const PENDING_MS = 10 * 60 * 1000; // 送信中のまま止まった記録は、10分でやり直せるようにする

// 同じ人に2回送らないための記録。1つのオブジェクトが順番に処理するので、同時に来ても重複しない。
export class Claims extends DurableObject {
  async fetch(request) {
    const { action, key } = await request.json();
    const now = Date.now();
    const current = await this.ctx.storage.get(key);
    if (action === "claim") {
      if (current && (current.status === "done" || now - current.at < PENDING_MS)) {
        return Response.json({ granted: false });
      }
      await this.ctx.storage.put(key, { status: "pending", at: now });
      return Response.json({ granted: true });
    }
    if (action === "done") await this.ctx.storage.put(key, { status: "done", at: now });
    if (action === "release") await this.ctx.storage.delete(key);
    return Response.json({ ok: true });
  }
}

function claims(env, action, key) {
  const stub = env.CLAIMS.get(env.CLAIMS.idFromName("all"));
  return stub.fetch("https://claims/", { method: "POST", body: JSON.stringify({ action, key }) }).then((r) => r.json());
}

const hex = (buf) => [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");

// scripts/ig_bot.py の hash_id と同じ値にする
export async function personKey(salt, mediaId, who) {
  const data = new TextEncoder().encode([salt, "person", mediaId, who].join("|"));
  return hex(await crypto.subtle.digest("SHA-256", data)).slice(0, 24);
}

async function validSignature(secret, body, header) {
  if (!header?.startsWith("sha256=")) return false;
  const key = await crypto.subtle.importKey("raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const expected = hex(await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body)));
  const given = header.slice(7);
  if (given.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ given.charCodeAt(i);
  return diff === 0;
}

async function graph(env, method, path, { params, body } = {}) {
  const query = params ? "?" + new URLSearchParams(params) : "";
  const res = await fetch(`${API}/${path}${query}`, {
    method,
    headers: { Authorization: `Bearer ${env.IG_ACCESS_TOKEN}`, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${(await res.text()).slice(0, 300)}`);
  return res.json();
}

// scripts/ig_bot.py の dm_text（dm_include_url が true のとき）と同じ文面
export function dmText(env, no) {
  return `コメントありがとうございます！\n動画の商品はこちらです👇\n${env.LINKS_PAGE}#no${no}\n※楽天のページに移動します`;
}

async function handleComment(env, value) {
  const who = String(value.from?.id ?? "");
  const text = value.text ?? "";
  // 自分のコメント・返信へのコメント・空・URL入りには送らない（連投は下の記録で1回にまとまる）
  if (!who || who === env.IG_USER_ID || value.from?.username === env.IG_USERNAME || value.parent_id || !text.trim() || URL_RE.test(text)) return;

  const mediaId = value.media.id;
  const media = await graph(env, "GET", mediaId, { params: { fields: "caption,timestamp,media_product_type" } });
  if (media.media_product_type !== "REELS") return;
  const age = Date.now() - Date.parse(media.timestamp.replace(/([+-]\d{2})(\d{2})$/, "$1:$2"));
  if (age > Number(env.REEL_WINDOW_DAYS) * 86400000) return;
  const found = NO_RE.exec(media.caption ?? "");
  if (!found) return;
  const no = Number(found[1]);
  const links = await (await fetch(`${env.LINKS_PAGE}links.json`, { cf: { cacheTtl: 0 } })).json();
  if (!links.items.some((i) => i.no === no)) return;

  const key = await personKey(env.IG_STATE_SALT, mediaId, who);
  if (!(await claims(env, "claim", key)).granted) return;
  try {
    await graph(env, "POST", `${env.IG_USER_ID}/messages`, {
      body: { recipient: { comment_id: value.comment_id ?? value.id }, message: { text: dmText(env, no) } },
    });
  } catch (e) {
    // 記録を取り消す。5分おきのボットがやり直す
    await claims(env, "release", key);
    throw e;
  }
  await claims(env, "done", key);
}

async function handleEvents(env, payload) {
  for (const entry of payload.entry ?? []) {
    for (const change of entry.changes ?? []) {
      if (change.field !== "comments") continue;
      try {
        await handleComment(env, change.value);
      } catch (e) {
        console.error(`コメントの処理に失敗：${e.message}`);
      }
    }
  }
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (url.pathname === "/webhook" && request.method === "GET") {
      const ok = url.searchParams.get("hub.mode") === "subscribe"
        && url.searchParams.get("hub.verify_token") === env.VERIFY_TOKEN;
      return ok ? new Response(url.searchParams.get("hub.challenge")) : new Response("forbidden", { status: 403 });
    }
    if (url.pathname === "/webhook" && request.method === "POST") {
      const body = await request.text();
      if (!env.IG_APP_SECRET || !(await validSignature(env.IG_APP_SECRET, body, request.headers.get("X-Hub-Signature-256")))) {
        return new Response("forbidden", { status: 403 });
      }
      // Meta にはすぐ返事をして、送信は裏で続ける
      ctx.waitUntil(handleEvents(env, JSON.parse(body)));
      return new Response("ok");
    }
    if (url.pathname === "/claim" && request.method === "POST") {
      if (!env.CLAIM_SECRET || request.headers.get("Authorization") !== `Bearer ${env.CLAIM_SECRET}`) {
        return new Response("forbidden", { status: 403 });
      }
      const { action, key } = await request.json();
      return Response.json(await claims(env, action, key));
    }
    return new Response("not found", { status: 404 });
  },
};
