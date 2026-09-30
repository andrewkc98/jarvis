"""Deterministic tests for hud/voice.js, run under Node with fully fake primitives.

No DOM, browser, microphone, speaker, or network is used: every platform dependency is a
fake injected into ``createBrowserVoiceController``.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

VOICE_JS = Path(__file__).resolve().parents[1] / "hud" / "voice.js"

HARNESS = r"""
const assert = require("assert");
const V = require(process.argv[2]);

const flush = async () => { for (let i = 0; i < 25; i++) await new Promise((r) => setImmediate(r)); };
const UUID_A = "11111111-1111-4111-8111-111111111111";
const UUID_B = "22222222-2222-4222-8222-222222222222";

class FakeBlob {
  constructor(parts, opts) {
    this.parts = parts; this.type = (opts && opts.type) || "";
    this.size = parts.reduce((n, p) => n + (p.size !== undefined ? p.size : p.byteLength || 0), 0);
  }
}
const chunk = (n) => ({ size: n });

function makeEnv(over) {
  over = over || {};
  const env = { calls: [], updates: [], fetches: [], timers: [], rafs: [], clearedTimers: [], cancelledRafs: [],
    streams: [], contexts: [], recorders: [], audios: [], created: [], revoked: [], uuids: [UUID_A, UUID_B] };
  const supported = over.supported || ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus",
    "audio/ogg", "audio/mp4;codecs=mp4a.40.2", "audio/mp4"];
  env.gumCount = 0;
  env.gumImpl = over.gum || (async () => {
    const tracks = [{ stopped: 0, stop() { this.stopped++; } }];
    const s = { tracks, getTracks() { return tracks; } };
    env.streams.push(s);
    return s;
  });
  class Rec {
    constructor(stream, opts) {
      this.stream = stream; this.opts = opts; this.state = "inactive";
      this.mimeType = over.recMime !== undefined ? over.recMime : opts.mimeType;
      this.ondataavailable = null; this.onstop = null; this.onerror = null;
      this.stopCalls = 0; env.recorders.push(this);
    }
    start(slice) { this.state = "recording"; this.slice = slice; }
    stop() { this.stopCalls++; if (this.state === "inactive") return; this.state = "inactive";
      if (this.onstop) this.onstop(); }
    emitData(n) { if (this.ondataavailable) this.ondataavailable({ data: chunk(n) }); }
  }
  Rec.isTypeSupported = (t) => { env.calls.push("isTypeSupported:" + t); return supported.includes(t); };
  class Ctx {
    constructor() { this.closed = 0; env.contexts.push(this); this.level = over.level || 0.1; }
    createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
    createAnalyser() { const c = this; return { fftSize: 4, getFloatTimeDomainData(buf) { buf.fill(c.level); } }; }
    close() { this.closed++; return Promise.resolve(); }
  }
  class FakeAudio {
    constructor(url) { this.url = url; this.paused = true; this.currentTime = 5; this.pauseCalls = 0; this.playCalls = 0;
      this.onended = null; this.onerror = null; env.audios.push(this); }
    play() { this.playCalls++; this.paused = false; return over.playReject ? Promise.reject(new Error("blocked")) : Promise.resolve(); }
    pause() { this.pauseCalls++; this.paused = true; }
  }
  class FakeAbort {
    constructor() { this.aborted = false; this.signal = { aborted: false }; }
    abort() { this.aborted = true; this.signal.aborted = true; }
  }
  env.fetchImpl = over.fetch || (async (url, init) => {
    if (url.includes("/voice-turn")) return jsonRes(200, { turn_id: env.lastId, transcript: "hello", response: "hi there" });
    if (url.includes("/voice-speech")) return { ok: true, status: 200, arrayBuffer: async () => ({ byteLength: 44 }) };
    return jsonRes(200, { cancelled: true });
  });
  env.deps = {
    mediaDevices: { getUserMedia: async (c) => { env.gumCount++; env.calls.push("gum:" + JSON.stringify(c)); return env.gumImpl(c); } },
    MediaRecorder: Rec, AudioContext: Ctx, Blob: FakeBlob, Audio: FakeAudio, AbortController: FakeAbort,
    URL: { createObjectURL: (b) => { const u = "blob:" + (env.created.length + 1); env.created.push({ url: u, blob: b }); return u; },
           revokeObjectURL: (u) => { env.revoked.push(u); } },
    fetch: (url, init) => { const rec = { url, init }; env.fetches.push(rec);
      if (init && init.signal && init.signal.aborted) return Promise.reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
      return env.fetchImpl(url, init); },
    setTimeout: (fn, ms) => { const t = { fn, ms, id: env.timers.length + 1 }; env.timers.push(t); return t.id; },
    clearTimeout: (id) => { env.clearedTimers.push(id); },
    requestAnimationFrame: (fn) => { const id = env.rafs.length + 1; env.rafs.push({ fn, id }); return id; },
    cancelAnimationFrame: (id) => { env.cancelledRafs.push(id); },
    randomUUID: () => { const u = env.uuids.shift(); env.lastId = u; return u; },
    apiBase: "http://127.0.0.1:8765/", maxUploadBytes: over.maxUploadBytes || 8388608, maxDurationSeconds: 30,
    acceptedMediaTypes: ["audio/webm", "audio/ogg", "audio/mp4"],
    speak: over.speak || false,
    onUpdate: (s) => env.updates.push(s)
  };
  env.ctl = V.createBrowserVoiceController(env.deps);
  env.last = () => env.updates[env.updates.length - 1];
  return env;
}
function jsonRes(status, body) { return { ok: status >= 200 && status < 300, status, json: async () => body }; }
function deferred() { let resolve, reject; const promise = new Promise((a, b) => { resolve = a; reject = b; }); return { promise, resolve, reject }; }
async function record(env, bytes) {
  assert.strictEqual(await env.ctl.start(), true);
  env.recorders[0].emitData(bytes === undefined ? 100 : bytes);
}
function assertReleasedOnce(env) {
  assert.strictEqual(env.streams[0].tracks[0].stopped, 1, "track stopped once");
  assert.strictEqual(env.contexts[0].closed, 1, "context closed once");
  const r = env.recorders[0];
  assert.strictEqual(r.ondataavailable, null); assert.strictEqual(r.onstop, null); assert.strictEqual(r.onerror, null);
}

const tests = {};
const t = (name, fn) => { tests[name] = fn; };

t("mime_selection_order_and_fallback", async () => {
  const mk = (list) => { const R = { isTypeSupported: (x) => list.includes(x) }; return R; };
  assert.strictEqual(V.selectRecorderMimeType(mk(["audio/webm;codecs=opus", "audio/ogg"])), "audio/webm;codecs=opus");
  assert.strictEqual(V.selectRecorderMimeType(mk(["audio/webm", "audio/ogg;codecs=opus"])), "audio/webm");
  assert.strictEqual(V.selectRecorderMimeType(mk(["audio/ogg;codecs=opus", "audio/mp4"])), "audio/ogg;codecs=opus");
  assert.strictEqual(V.selectRecorderMimeType(mk(["audio/mp4;codecs=mp4a.40.2", "audio/mp4"])), "audio/mp4;codecs=mp4a.40.2");
  assert.strictEqual(V.selectRecorderMimeType(mk(["audio/mp4"])), "audio/mp4");
  assert.strictEqual(V.selectRecorderMimeType(mk(["audio/wav", "audio/x-matroska"])), null);
  assert.strictEqual(V.selectRecorderMimeType(mk(["audio/webm"]), ["audio/ogg"]), null);
  assert.strictEqual(V.selectRecorderMimeType(undefined), null);
  assert.strictEqual(V.selectRecorderMimeType({}), null);
  assert.strictEqual(V.normalizeMediaType(" Audio/WebM ; codecs=opus"), "audio/webm");
});

t("feature_detection_is_pure_and_no_permission_before_click", async () => {
  const env = makeEnv();
  assert.strictEqual(V.detectFeatures(env.deps).supported, true);
  assert.strictEqual(V.detectFeatures({}).supported, false);
  assert.strictEqual(env.gumCount, 0);
  assert.strictEqual(env.recorders.length, 0);
  assert.strictEqual(env.contexts.length, 0);
  assert.strictEqual(env.fetches.length, 0);
  assert.strictEqual(env.timers.length, 0);
  assert.strictEqual(env.rafs.length, 0);
  assert.deepStrictEqual(env.updates, []);
  assert.ok(!env.calls.some((c) => c.startsWith("gum:")));
});

t("unsupported_browser_never_requests_microphone", async () => {
  const env = makeEnv({ supported: ["audio/wav"] });
  assert.strictEqual(await env.ctl.start(), false);
  assert.strictEqual(env.gumCount, 0);
  assert.strictEqual(env.last().phase, "error");
  assert.strictEqual(env.last().error, "unsupported_browser");
});

t("permission_error_categories", async () => {
  const cases = { NotAllowedError: "permission_denied", SecurityError: "permission_denied", NotFoundError: "no_microphone",
    OverconstrainedError: "no_microphone", NotReadableError: "microphone_unavailable", AbortError: "microphone_unavailable",
    TypeError: "capture_failed" };
  for (const [name, code] of Object.entries(cases)) {
    const env = makeEnv({ gum: async () => { throw Object.assign(new Error("secret detail"), { name }); } });
    assert.strictEqual(await env.ctl.start(), false);
    assert.strictEqual(env.last().error, code, name);
    assert.strictEqual(env.last().phase, "error");
    assert.ok(!JSON.stringify(env.updates).includes("secret detail"));
    // a retry is allowed after an error
    env.uuids.push("33333333-3333-4333-8333-333333333333");
    assert.strictEqual(await env.ctl.start(), false);
    assert.strictEqual(env.gumCount, 2);
  }
});

t("start_lifecycle_level_and_request", async () => {
  const env = makeEnv({ level: 0.1 });
  const p = env.ctl.start();
  assert.strictEqual(env.last().phase, "requesting");
  assert.strictEqual(env.last().busy, true);
  assert.strictEqual(await p, true);
  assert.strictEqual(env.last().phase, "recording");
  assert.strictEqual(env.last().turnId, UUID_A);
  assert.strictEqual(env.gumCount, 1);
  assert.deepStrictEqual(env.calls.filter((c) => c.startsWith("gum:")), ['gum:{"audio":true}']);
  assert.strictEqual(env.recorders[0].opts.mimeType, "audio/webm;codecs=opus");
  assert.strictEqual(await env.ctl.start(), false, "second start ignored");
  assert.strictEqual(env.gumCount, 1);
  env.rafs[0].fn();
  assert.ok(Math.abs(env.last().level - 0.3) < 1e-6);
  assert.strictEqual(env.rafs.length, 2);
  env.contexts[0].level = 10;
  env.rafs[1].fn();
  assert.strictEqual(env.last().level, 1);
  assert.strictEqual(env.timers[0].ms, 30000);
});

t("stop_and_send_uploads_exact_request_and_cleans_once", async () => {
  const env = makeEnv();
  await record(env, 100);
  env.recorders[0].emitData(50);
  assert.strictEqual(env.ctl.stopAndSend(), true);
  assert.strictEqual(env.ctl.stopAndSend(), false);
  await flush();
  assertReleasedOnce(env);
  assert.strictEqual(env.fetches.length, 1);
  const f = env.fetches[0];
  assert.strictEqual(f.url, "http://127.0.0.1:8765/voice-turn?turn_id=" + encodeURIComponent(UUID_A));
  assert.strictEqual(f.init.method, "POST");
  assert.deepStrictEqual(f.init.headers, { "Content-Type": "audio/webm;codecs=opus" });
  assert.ok(f.init.body instanceof FakeBlob);
  assert.strictEqual(f.init.body.size, 150);
  assert.strictEqual(f.init.body.type, "audio/webm;codecs=opus");
  assert.strictEqual(f.init.credentials, "omit");
  assert.strictEqual(env.last().phase, "idle");
  assert.strictEqual(env.last().transcript, "hello");
  assert.strictEqual(env.last().response, "hi there");
  assert.strictEqual(env.last().busy, false);
  assert.strictEqual(env.clearedTimers.length, 1);
  assert.strictEqual(env.cancelledRafs.length, 1);
  assert.strictEqual(env.fetches.length, 1, "no synthesis when speak is off");
});

t("actual_recorder_mime_is_used_and_never_relabelled", async () => {
  let env = makeEnv({ recMime: "audio/mp4" });
  await record(env);
  env.ctl.stopAndSend(); await flush();
  assert.deepStrictEqual(env.fetches[0].init.headers, { "Content-Type": "audio/mp4" });
  assert.strictEqual(env.fetches[0].init.body.type, "audio/mp4");
  env = makeEnv({ recMime: "audio/x-weird" });
  await record(env);
  env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.fetches.length, 0);
  assert.strictEqual(env.last().error, "unsupported_browser");
  assertReleasedOnce(env);
  env = makeEnv({ recMime: "" });
  await record(env);
  env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.fetches[0].init.headers["Content-Type"], "audio/webm;codecs=opus");
});

t("empty_recording_fails_locally", async () => {
  const env = makeEnv();
  assert.strictEqual(await env.ctl.start(), true);
  env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.fetches.length, 0);
  assert.strictEqual(env.last().error, "empty_recording");
  assertReleasedOnce(env);
});

t("oversize_fails_locally_without_upload", async () => {
  const env = makeEnv({ maxUploadBytes: 1000 });
  await record(env, 600);
  env.recorders[0].emitData(401);
  await flush();
  assert.strictEqual(env.fetches.length, 0);
  assert.strictEqual(env.last().error, "recording_too_large");
  assert.strictEqual(env.last().phase, "error");
  assertReleasedOnce(env);
  // exactly at the bound is allowed
  const ok = makeEnv({ maxUploadBytes: 1000 });
  await record(ok, 1000);
  ok.ctl.stopAndSend(); await flush();
  assert.strictEqual(ok.fetches.length, 1);
});

t("auto_stop_at_thirty_seconds_sends", async () => {
  const env = makeEnv();
  await record(env);
  assert.strictEqual(env.timers.length, 1);
  env.timers[0].fn();
  await flush();
  assert.strictEqual(env.fetches.length, 1);
  assert.strictEqual(env.last().transcript, "hello");
  assertReleasedOnce(env);
});

t("recorder_error_releases_everything", async () => {
  const env = makeEnv();
  await record(env);
  const r = env.recorders[0];
  r.onerror({});
  assert.strictEqual(env.last().error, "capture_failed");
  assertReleasedOnce(env);
  assert.strictEqual(env.clearedTimers.length, 1);
  assert.strictEqual(env.cancelledRafs.length, 1);
});

t("backend_error_mapping", async () => {
  const map = [[409, "turn_in_progress"], [422, "invalid_turn_id"], [503, "browser_voice_unavailable"], [503, "voice_model_not_configured"],
    [503, "decoder_unavailable"], [415, "unsupported_audio_type"], [422, "empty_audio"], [413, "audio_too_large"],
    [504, "audio_decode_timeout"], [422, "audio_decode_failed"], [413, "audio_too_long"], [422, "no_speech_detected"],
    [502, "voice_turn_failed"], [504, "voice_turn_timeout"]];
  for (const [status, code] of map) {
    const env = makeEnv({ fetch: async () => jsonRes(status, { error: code, detail: "LEAK" }) });
    await record(env); env.ctl.stopAndSend(); await flush();
    assert.strictEqual(env.last().error, code);
    assert.strictEqual(env.last().phase, "error");
    assert.ok(!JSON.stringify(env.updates).includes("LEAK"));
  }
  let env = makeEnv({ fetch: async () => jsonRes(500, { error: "weird_unknown" }) });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().error, "voice_turn_failed");
  env = makeEnv({ fetch: async () => { throw new TypeError("Failed to fetch"); } });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().error, "network_error");
  env = makeEnv({ fetch: async () => jsonRes(200, { turn_id: "other", transcript: "a", response: "b" }) });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().error, "voice_turn_failed");
  assert.strictEqual(env.last().transcript, "");
  env = makeEnv({ fetch: async () => ({ ok: true, status: 200, json: async () => { throw new Error("bad"); } }) });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().error, "voice_turn_failed");
});

t("cancel_while_requesting_discards_late_stream", async () => {
  const d = deferred();
  const env = makeEnv({ gum: () => d.promise });
  const p = env.ctl.start();
  assert.strictEqual(env.last().phase, "requesting");
  assert.strictEqual(await env.ctl.cancel(), true);
  assert.strictEqual(env.last().phase, "idle");
  const tracks = [{ stopped: 0, stop() { this.stopped++; } }];
  d.resolve({ getTracks: () => tracks });
  assert.strictEqual(await p, false);
  assert.strictEqual(tracks[0].stopped, 1);
  assert.strictEqual(env.recorders.length, 0);
  assert.strictEqual(env.fetches.length, 0);
  assert.strictEqual(env.last().phase, "idle");
});

t("cancel_while_recording_is_local_only", async () => {
  const env = makeEnv();
  await record(env);
  assert.strictEqual(await env.ctl.cancel(), true);
  await flush();
  assert.strictEqual(env.fetches.length, 0, "no upload and no voice-cancel");
  assertReleasedOnce(env);
  assert.strictEqual(env.last().phase, "idle");
  assert.strictEqual(env.last().transcript, "");
  assert.strictEqual(env.clearedTimers.length, 1);
  assert.strictEqual(env.cancelledRafs.length, 1);
  assert.strictEqual(await env.ctl.cancel(), false);
});

t("cancel_after_stop_before_upload_is_local_only", async () => {
  const env = makeEnv();
  await record(env);
  // make recorder stop asynchronous so cancel lands before upload starts
  env.recorders[0].stop = function () { this.stopCalls++; };
  env.ctl.stopAndSend();
  assert.strictEqual(env.last().phase, "processing");
  assert.strictEqual(await env.ctl.cancel(), true);
  await flush();
  assert.strictEqual(env.fetches.length, 0);
  assertReleasedOnce(env);
  assert.strictEqual(env.last().phase, "idle");
});

t("cancel_during_upload_aborts_and_posts_exact_id", async () => {
  const d = deferred();
  const env = makeEnv({ fetch: async (url) => url.includes("/voice-turn") ? d.promise : jsonRes(200, { cancelled: true }) });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().phase, "processing");
  const mainInit = env.fetches[0].init;
  const n = env.updates.length;
  assert.strictEqual(await env.ctl.cancel(), true);
  assert.strictEqual(mainInit.signal.aborted, true);
  assert.strictEqual(env.fetches.length, 2);
  const c = env.fetches[1];
  assert.strictEqual(c.url, "http://127.0.0.1:8765/voice-cancel");
  assert.strictEqual(c.init.method, "POST");
  assert.deepStrictEqual(c.init.headers, { "Content-Type": "application/json" });
  assert.strictEqual(c.init.body, JSON.stringify({ turn_id: UUID_A }));
  assert.strictEqual(env.last().phase, "idle");
  // late response ignored
  d.resolve(jsonRes(200, { turn_id: UUID_A, transcript: "late", response: "late" }));
  await flush();
  assert.strictEqual(env.last().transcript, "");
  assert.strictEqual(env.last().phase, "idle");
  assert.strictEqual(env.fetches.length, 2);
});

t("cancel_request_failure_does_not_resurrect_turn", async () => {
  const d = deferred();
  const env = makeEnv({ fetch: async (url) => { if (url.includes("/voice-turn")) return d.promise; throw new TypeError("down"); } });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(await env.ctl.cancel(), true);
  await flush();
  assert.strictEqual(env.last().phase, "idle");
  assert.strictEqual(env.last().error, null);
  const cancelFail = makeEnv({ fetch: async (url) => { if (url.includes("/voice-turn")) return d.promise; return jsonRes(409, { error: "no_matching_turn" }); } });
  await record(cancelFail); cancelFail.ctl.stopAndSend(); await flush();
  await cancelFail.ctl.cancel(); await flush();
  assert.strictEqual(cancelFail.last().phase, "idle");
  assert.strictEqual(cancelFail.last().error, null);
});

t("late_response_from_old_turn_ignored_after_new_turn", async () => {
  const d = deferred();
  let n = 0;
  const env = makeEnv({ fetch: async (url) => {
    if (url.includes("/voice-turn")) { n++; if (n === 1) return d.promise; return jsonRes(200, { turn_id: UUID_B, transcript: "second", response: "two" }); }
    return jsonRes(200, { cancelled: true });
  } });
  await record(env); env.ctl.stopAndSend(); await flush();
  await env.ctl.cancel();
  env.recorders.length = 0;
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().transcript, "second");
  d.resolve(jsonRes(200, { turn_id: UUID_A, transcript: "OLD", response: "OLD" }));
  await flush();
  assert.strictEqual(env.last().transcript, "second");
  assert.strictEqual(env.last().response, "two");
});

t("response_delivered_before_speech_then_autoplay", async () => {
  const sp = deferred();
  const env = makeEnv({ speak: true, fetch: async (url) => {
    if (url.includes("/voice-turn")) return jsonRes(200, { turn_id: UUID_A, transcript: "hello", response: "hi there" });
    return sp.promise;
  } });
  await record(env); env.ctl.stopAndSend(); await flush();
  const before = env.updates.findIndex((u) => u.transcript === "hello" && u.phase === "idle");
  const synth = env.updates.findIndex((u) => u.phase === "synthesizing");
  assert.ok(before >= 0 && synth > before, "text update precedes synthesis");
  assert.strictEqual(env.last().busy, true);
  assert.strictEqual(env.last().response, "hi there");
  const s = env.fetches[1];
  assert.strictEqual(s.url, "http://127.0.0.1:8765/voice-speech");
  assert.strictEqual(s.init.method, "POST");
  assert.deepStrictEqual(s.init.headers, { "Content-Type": "application/json" });
  assert.strictEqual(s.init.body, JSON.stringify({ text: "hi there" }));
  sp.resolve({ ok: true, status: 200, arrayBuffer: async () => ({ byteLength: 44 }) });
  await flush();
  assert.strictEqual(env.created.length, 1);
  assert.strictEqual(env.created[0].blob.type, "audio/wav");
  assert.strictEqual(env.audios.length, 1);
  assert.strictEqual(env.audios[0].url, env.created[0].url);
  assert.strictEqual(env.audios[0].playCalls, 1);
  assert.strictEqual(env.last().phase, "playing");
  assert.strictEqual(env.last().playing, true);
  env.audios[0].onended();
  assert.strictEqual(env.last().phase, "idle");
  assert.strictEqual(env.last().canPlay, true);
  assert.strictEqual(env.last().response, "hi there");
  assert.deepStrictEqual(env.revoked, []);
});

t("autoplay_rejection_preserves_text_and_offers_play", async () => {
  const env = makeEnv({ speak: true, playReject: true });
  await record(env); env.ctl.stopAndSend(); await flush();
  const s = env.last();
  assert.strictEqual(s.phase, "idle");
  assert.strictEqual(s.notice, "autoplay_blocked");
  assert.strictEqual(s.error, null);
  assert.strictEqual(s.canPlay, true);
  assert.strictEqual(s.transcript, "hello");
  assert.strictEqual(s.response, "hi there");
  assert.strictEqual(s.busy, false);
  assert.deepStrictEqual(env.revoked, []);
});

t("play_stop_replay_and_revocation", async () => {
  const env = makeEnv({ speak: true });
  await record(env); env.ctl.stopAndSend(); await flush();
  const el = env.audios[0];
  assert.strictEqual(env.last().phase, "playing");
  assert.strictEqual(env.ctl.stopPlayback(), true);
  assert.strictEqual(el.paused, true); assert.strictEqual(el.currentTime, 0);
  assert.strictEqual(env.last().phase, "idle"); assert.strictEqual(env.last().canPlay, true);
  assert.strictEqual(env.ctl.stopPlayback(), false);
  assert.strictEqual(env.ctl.play(), true);
  await flush();
  assert.strictEqual(el.playCalls, 2);
  assert.strictEqual(env.audios.length, 1, "replay reuses the same element");
  assert.strictEqual(env.last().phase, "playing");
  assert.strictEqual(env.ctl.play(), false, "no overlapping play");
  el.onended();
  assert.strictEqual(env.ctl.play(), true); await flush();
  assert.strictEqual(el.playCalls, 3);
  assert.deepStrictEqual(env.revoked, []);
  // a new recording replaces the previous reply and revokes its URL once
  el.onended();
  env.recorders.length = 0; env.streams.length = 0; env.contexts.length = 0;
  await record(env);
  assert.deepStrictEqual(env.revoked, ["blob:1"]);
  assert.strictEqual(env.last().canPlay, false);
  assert.strictEqual(env.last().transcript, "");
  await env.ctl.cancel();
  assert.deepStrictEqual(env.revoked, ["blob:1"]);
});

t("cancel_during_playback_and_synthesis", async () => {
  let env = makeEnv({ speak: true });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(await env.ctl.cancel(), true);
  assert.strictEqual(env.audios[0].paused, true);
  assert.strictEqual(env.audios[0].currentTime, 0);
  assert.deepStrictEqual(env.revoked, ["blob:1"]);
  assert.strictEqual(env.last().canPlay, false);
  assert.strictEqual(env.last().response, "hi there");
  assert.strictEqual(env.fetches.length, 2, "no voice-cancel for playback");
  env.ctl.dispose();
  assert.deepStrictEqual(env.revoked, ["blob:1"]);

  const sp = deferred();
  env = makeEnv({ speak: true, fetch: async (url) => url.includes("/voice-turn")
    ? jsonRes(200, { turn_id: UUID_A, transcript: "hello", response: "hi there" }) : sp.promise });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().phase, "synthesizing");
  const init = env.fetches[1].init;
  assert.strictEqual(await env.ctl.cancel(), true);
  assert.strictEqual(init.signal.aborted, true);
  assert.strictEqual(env.fetches.length, 2, "speech cancel does not hit /voice-cancel");
  sp.resolve({ ok: true, status: 200, arrayBuffer: async () => ({ byteLength: 44 }) });
  await flush();
  assert.strictEqual(env.created.length, 0);
  assert.strictEqual(env.last().phase, "idle");
  assert.strictEqual(env.last().response, "hi there");
});

t("speech_failures_keep_text", async () => {
  const cases = [[502, "speech_failed", "speech_failed"], [409, "speech_busy", "speech_busy"],
    [503, "voice_model_not_configured", "voice_model_not_configured"], [422, "invalid_speech_text", "speech_failed"]];
  for (const [status, err, expected] of cases) {
    const env = makeEnv({ speak: true, fetch: async (url) => url.includes("/voice-turn")
      ? jsonRes(200, { turn_id: UUID_A, transcript: "hello", response: "hi there" }) : jsonRes(status, { error: err }) });
    await record(env); env.ctl.stopAndSend(); await flush();
    assert.strictEqual(env.last().error, expected);
    assert.strictEqual(env.last().transcript, "hello");
    assert.strictEqual(env.last().response, "hi there");
    assert.strictEqual(env.created.length, 0);
  }
  const env = makeEnv({ speak: true, fetch: async (url) => { if (url.includes("/voice-turn")) return jsonRes(200, { turn_id: UUID_A, transcript: "hello", response: "hi there" }); throw new TypeError("x"); } });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.last().error, "speech_failed");
});

t("speak_toggle_is_read_at_response_time", async () => {
  let flag = false;
  const env = makeEnv({ speak: () => flag });
  env.ctl.setSpeak(true);
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.fetches.length, 2);
});

t("empty_response_skips_synthesis", async () => {
  const env = makeEnv({ speak: true, fetch: async () => jsonRes(200, { turn_id: UUID_A, transcript: "hello", response: "" }) });
  await record(env); env.ctl.stopAndSend(); await flush();
  assert.strictEqual(env.fetches.length, 1);
  assert.strictEqual(env.last().phase, "idle");
});

t("dispose_during_recording_releases_once_and_silences_updates", async () => {
  const env = makeEnv();
  await record(env);
  const n = env.updates.length;
  env.ctl.dispose(); env.ctl.dispose();
  await flush();
  assertReleasedOnce(env);
  assert.strictEqual(env.clearedTimers.length, 1);
  assert.strictEqual(env.cancelledRafs.length, 1);
  assert.strictEqual(env.updates.length, n, "no updates after dispose");
  assert.strictEqual(env.fetches.length, 0);
  assert.strictEqual(await env.ctl.start(), false);
  assert.strictEqual(env.ctl.stopAndSend(), false);
  assert.strictEqual(await env.ctl.cancel(), false);
  assert.strictEqual(env.ctl.play(), false);
});

t("dispose_during_request_aborts_and_posts_cancel", async () => {
  const d = deferred();
  const env = makeEnv({ fetch: async (url) => url.includes("/voice-turn") ? d.promise : jsonRes(200, {}) });
  await record(env); env.ctl.stopAndSend(); await flush();
  const init = env.fetches[0].init;
  const n = env.updates.length;
  env.ctl.dispose();
  assert.strictEqual(init.signal.aborted, true);
  assert.strictEqual(env.fetches.length, 2);
  assert.strictEqual(env.fetches[1].url, "http://127.0.0.1:8765/voice-cancel");
  assert.strictEqual(env.fetches[1].init.body, JSON.stringify({ turn_id: UUID_A }));
  d.resolve(jsonRes(200, { turn_id: UUID_A, transcript: "late", response: "late" }));
  await flush();
  assert.strictEqual(env.updates.length, n);
});

t("dispose_during_requesting_stops_late_stream", async () => {
  const d = deferred();
  const env = makeEnv({ gum: () => d.promise });
  const p = env.ctl.start();
  env.ctl.dispose();
  const tracks = [{ stopped: 0, stop() { this.stopped++; } }];
  d.resolve({ getTracks: () => tracks });
  await p;
  assert.strictEqual(tracks[0].stopped, 1);
  assert.strictEqual(env.recorders.length, 0);
});

t("dispose_revokes_audio_once", async () => {
  const env = makeEnv({ speak: true });
  await record(env); env.ctl.stopAndSend(); await flush();
  env.ctl.dispose(); env.ctl.dispose();
  assert.deepStrictEqual(env.revoked, ["blob:1"]);
  assert.strictEqual(env.audios[0].paused, true);
  assert.strictEqual(env.audios[0].onended, null);
});

t("no_forbidden_apis_or_hosts_in_source", async () => {
  const src = require("fs").readFileSync(process.argv[2], "utf8");
  for (const bad of ["SpeechRecognition", "speechSynthesis", "localStorage", "sessionStorage", "indexedDB", "document.cookie", "XMLHttpRequest", "WebSocket", "http://", "https://"]) {
    assert.ok(!src.includes(bad), bad);
  }
});

(async () => {
  const results = {};
  for (const [name, fn] of Object.entries(tests)) {
    try { await fn(); results[name] = null; } catch (e) { results[name] = String(e && e.stack || e); }
  }
  process.stdout.write(JSON.stringify(results));
})();
"""


@pytest.fixture(scope="module")
def harness_results(tmp_path_factory):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    script = tmp_path_factory.mktemp("hud_voice") / "harness.js"
    script.write_text(HARNESS, encoding="utf-8")
    completed = subprocess.run(
        [node, str(script), str(VOICE_JS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


TEST_NAMES = [
    "mime_selection_order_and_fallback",
    "feature_detection_is_pure_and_no_permission_before_click",
    "unsupported_browser_never_requests_microphone",
    "permission_error_categories",
    "start_lifecycle_level_and_request",
    "stop_and_send_uploads_exact_request_and_cleans_once",
    "actual_recorder_mime_is_used_and_never_relabelled",
    "empty_recording_fails_locally",
    "oversize_fails_locally_without_upload",
    "auto_stop_at_thirty_seconds_sends",
    "recorder_error_releases_everything",
    "backend_error_mapping",
    "cancel_while_requesting_discards_late_stream",
    "cancel_while_recording_is_local_only",
    "cancel_after_stop_before_upload_is_local_only",
    "cancel_during_upload_aborts_and_posts_exact_id",
    "cancel_request_failure_does_not_resurrect_turn",
    "late_response_from_old_turn_ignored_after_new_turn",
    "response_delivered_before_speech_then_autoplay",
    "autoplay_rejection_preserves_text_and_offers_play",
    "play_stop_replay_and_revocation",
    "cancel_during_playback_and_synthesis",
    "speech_failures_keep_text",
    "speak_toggle_is_read_at_response_time",
    "empty_response_skips_synthesis",
    "dispose_during_recording_releases_once_and_silences_updates",
    "dispose_during_request_aborts_and_posts_cancel",
    "dispose_during_requesting_stops_late_stream",
    "dispose_revokes_audio_once",
    "no_forbidden_apis_or_hosts_in_source",
]


def test_harness_covers_declared_names(harness_results):
    assert sorted(harness_results) == sorted(TEST_NAMES)


@pytest.mark.parametrize("name", TEST_NAMES)
def test_voice_controller_scenario(harness_results, name):
    assert harness_results.get(name) is None, harness_results.get(name)


def test_voice_js_syntax_valid():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    completed = subprocess.run(
        [node, "--check", str(VOICE_JS)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout
