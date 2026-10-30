// 実行：node --test test.mjs（偽のAPIを相手に試す。本物のインスタには接続しない）
import assert from "node:assert/strict";
import { createHmac } from "node:crypto";
import { register } from "node:module";
import { test, beforeEach } from "node:test";

// cloudflare:workers を、テスト用の小さな代わりに差し替える
const stub = "export class DurableObject { constructor(ctx, env) { this.ctx = ctx; this.env = env; } }";
const hook = `export async function resolve(s, c, n) {
  if (s === "cloudflare:workers") return { url: "data:text/javascript," + encodeURIComponent(${JSON.stringify(stub)}), shortCircuit: true };
  return n(s, c);
}`;
register("data:text/javascript," + encodeURIComponent(hook));
const { default: worker, Claims, personKey, collectStats } = await import("./src/index.js");

const env = (claimObj) => ({
  IG_USER_ID: "99", IG_USERNAME: "wo_kix", LINKS_PAGE: "https://links.test/", REEL_WINDOW_DAYS: "14",
  IG_ACCESS_TOKEN: "t", IG_STATE_SALT: "s", IG_APP_SECRET: "app", VERIFY_TOKEN: "v", CLAIM_SECRET: "c", STATS_SECRET: "st",
  CLAIMS: { idFromName: () => "id", get: () => ({ fetch: (u, init) => claimObj.fetch(new Request(u, init)) }) },
});
function makeClaims() {
  const m = new Map();
  const storage = { get: async (k) => m.get(k), put: async (k, v) => m.set(k, v), delete: async (k) => m.delete(k), list: async ({ prefix }) => new Map([...m].filter(([k]) => k.startsWith(prefix))) };
  return { store: m, obj: new Claims({ storage }, {}) };
}
const sign = (body, secret = "app") => "sha256=" + createHmac("sha256", secret).update(body).digest("hex");
function event(id, text, who = "alice", extra = {}) {
  return { entry: [{ id: "99", changes: [{ field: "comments", value: { comment_id: id, text, from: { id: who, username: who }, media: { id: "m1" }, ...extra } }] }] };
}

let sent, failSend, media;
beforeEach(() => {
  sent = []; failSend = false;
  const yesterday = new Date(Date.now() - 86400000).toISOString().replace(/\.\d+Z$/, "+0000");
  media = { caption: "テスト No.999", timestamp: yesterday, media_product_type: "REELS" };
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    if (u.startsWith("https://links.test/")) return Response.json({ items: [{ no: 999 }] });
    if (u.includes("/m1?")) return Response.json(media);
    if (u.endsWith("/99/messages")) {
      if (failSend) return new Response("no", { status: 400 });
      sent.push(JSON.parse(init.body));
      return Response.json({ message_id: "x" });
    }
    throw new Error("想定外の通信 " + u);
  };
});
async function post(e, payload, sig) {
  const body = JSON.stringify(payload);
  const waits = [];
  const req = new Request("https://w/webhook", { method: "POST", body, headers: { "X-Hub-Signature-256": sig ?? sign(body) } });
  const res = await worker.fetch(req, e, { waitUntil: (p) => waits.push(p) });
  await Promise.all(waits);
  return res;
}

test("Meta の確認に答える", async () => {
  const e = env(makeClaims().obj);
  const ok = await worker.fetch(new Request("https://w/webhook?hub.mode=subscribe&hub.verify_token=v&hub.challenge=abc"), e, {});
  assert.equal(await ok.text(), "abc");
  const ng = await worker.fetch(new Request("https://w/webhook?hub.mode=subscribe&hub.verify_token=x&hub.challenge=abc"), e, {});
  assert.equal(ng.status, 403);
});

test("署名が違う連絡は受け付けない", async () => {
  const res = await post(env(makeClaims().obj), event("1", "おにぎり"), sign("偽物"));
  assert.equal(res.status, 403);
  assert.equal(sent.length, 0);
});

test("普通のコメントにDMを送る", async () => {
  await post(env(makeClaims().obj), event("1", "おにぎり"));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].recipient.comment_id, "1");
  assert.ok(sent[0].message.text.includes("https://links.test/#no999"));
});

test("コメントIDが id で届いても送れる", async () => {
  const payload = event("1", "おにぎり");
  const value = payload.entry[0].changes[0].value;
  value.id = value.comment_id;
  delete value.comment_id;
  await post(env(makeClaims().obj), payload);
  assert.equal(sent[0].recipient.comment_id, "1");
});

test("送らないコメント", async () => {
  const e = env(makeClaims().obj);
  await post(e, event("1", "https://example.com"));
  await post(e, event("2", "見て spam.com"));
  await post(e, event("3", "   "));
  await post(e, event("4", "自分", "99"));
  await post(e, event("5", "返信", "bob", { parent_id: "1" }));
  await post(e, event("6", "自分", "wo_kix"));
  assert.equal(sent.length, 0);
});

test("対象外の投稿には送らない", async () => {
  const e = env(makeClaims().obj);
  media.caption = "番号なし";
  await post(e, event("1", "a"));
  media.caption = "No.5"; // ページにない番号
  await post(e, event("2", "a"));
  media.caption = "No.999"; media.media_product_type = "FEED";
  await post(e, event("3", "a"));
  media.media_product_type = "REELS"; media.timestamp = "2026-01-01T00:00:00+0000"; // 古い
  await post(e, event("4", "a"));
  assert.equal(sent.length, 0);
});

test("同じ連絡が2回来ても、同時に来ても、同じ人の別コメントでも1通だけ", async () => {
  const e = env(makeClaims().obj);
  await post(e, event("1", "おにぎり"));
  await post(e, event("1", "おにぎり"));
  await post(e, event("2", "ラーメン"));
  await Promise.all([post(e, event("3", "a", "carol")), post(e, event("3", "a", "carol")), post(e, event("4", "b", "carol"))]);
  assert.equal(sent.length, 2);
});

test("送信に失敗したら記録を取り消し、やり直せる", async () => {
  const c = makeClaims(); const e = env(c.obj);
  failSend = true;
  await post(e, event("1", "おにぎり"));
  assert.equal(c.store.size, 0);
  failSend = false;
  await post(e, event("1", "おにぎり"));
  assert.equal(sent.length, 1);
});

test("送信中のまま止まった記録は10分でやり直せる", async () => {
  const c = makeClaims(); const e = env(c.obj);
  const key = await personKey("s", "m1", "alice");
  c.store.set(key, { status: "pending", at: Date.now() - 60000 });
  await post(e, event("1", "a"));
  assert.equal(sent.length, 0);
  c.store.set(key, { status: "pending", at: Date.now() - 11 * 60000 });
  await post(e, event("1", "a"));
  assert.equal(sent.length, 1);
});

test("ボット用の入口は合言葉が必要で、両方の仕組みで1回だけ", async () => {
  const c = makeClaims(); const e = env(c.obj);
  const call = (auth, action, key) => worker.fetch(new Request("https://w/claim", {
    method: "POST", headers: { Authorization: `Bearer ${auth}` }, body: JSON.stringify({ action, key }),
  }), e, {});
  assert.equal((await call("x", "claim", "k")).status, 403);
  const key = await personKey("s", "m1", "alice");
  assert.equal((await (await call("c", "claim", key)).json()).granted, true);
  await post(e, event("1", "a")); // ボットが送っている最中に連絡が来ても送らない
  assert.equal(sent.length, 0);
  await call("c", "done", key);
  assert.equal((await (await call("c", "claim", key)).json()).granted, false);
});

test("リールの数字を24時間後と1週間後に1回ずつ記録する", async () => {
  const c = makeClaims(); const e = env(c.obj);
  const posted = Date.parse("2026-10-01T00:00:00Z");
  let insightsFail = false, insightCalls = 0;
  globalThis.fetch = async (url) => {
    const u = String(url);
    if (u.includes("/99/media?")) return Response.json({ data: [
      { id: "r1", caption: "題名\nNo.1", timestamp: "2026-10-01T00:00:00+0000", permalink: "https://i/r1", media_product_type: "REELS", like_count: 5, comments_count: 2 },
      { id: "f1", caption: "写真", timestamp: "2026-10-01T00:00:00+0000", media_product_type: "FEED" },
    ] });
    if (u.includes("/r1/insights?")) {
      insightCalls++;
      if (insightsFail) return new Response("no", { status: 400 });
      return Response.json({ data: [{ name: "views", values: [{ value: 100 }] }, { name: "saved", values: [{ value: 3 }] }] });
    }
    throw new Error("想定外の通信 " + u);
  };
  await collectStats(e, posted + 23 * 3600000);
  assert.equal(c.store.size, 0); // まだ24時間たっていない
  await collectStats(e, posted + 25 * 3600000);
  await collectStats(e, posted + 26 * 3600000); // 同じ時点は2回記録しない
  assert.equal(c.store.size, 1);
  assert.equal(insightCalls, 1);
  const first = c.store.get("stats:r1:24h");
  assert.deepEqual([first.views, first.saved, first.likes, first.caption, first.hours_after], [100, 3, 5, "題名", 25]);
  insightsFail = true; // 再生数が読めなくても、いいねとコメントは残す
  await collectStats(e, posted + 7 * 24 * 3600000);
  const week = c.store.get("stats:r1:7d");
  assert.equal(week.comments, 2);
  assert.ok(week.insights_error);
  const res = await worker.fetch(new Request("https://w/stats", { headers: { Authorization: "Bearer st" } }), e, {});
  assert.equal((await res.json()).length, 2);
  assert.equal((await worker.fetch(new Request("https://w/stats", { headers: { Authorization: "Bearer x" } }), e, {})).status, 403);
});
