/*
 * JARVIS HUD browser voice controller.
 *
 * DOM-free, no-build module. Exposes one `window.JarvisVoice` namespace and, when a CommonJS
 * `module` exists (Node tests), the same object through `module.exports`.
 *
 * Every platform primitive is injected through `createBrowserVoiceController(deps)`; nothing is
 * touched before `start()` except pure feature/MIME detection. Audio only ever goes to the
 * injected loopback API base. No browser speech APIs or browser storage are used.
 */
(function () {
  "use strict";

  var MAX_UPLOAD_BYTES = 8388608;
  var MAX_DURATION_SECONDS = 30;
  var DEFAULT_ACCEPTED_TYPES = ["audio/webm", "audio/ogg", "audio/mp4"];
  var RECORDER_CANDIDATES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/ogg",
    "audio/mp4;codecs=mp4a.40.2",
    "audio/mp4"
  ];
  var BUSY_PHASES = { requesting: 1, recording: 1, processing: 1, synthesizing: 1, playing: 1 };
  var BACKEND_TURN_ERRORS = {
    invalid_turn_id: "invalid_turn_id",
    turn_in_progress: "turn_in_progress",
    browser_voice_unavailable: "browser_voice_unavailable",
    voice_model_not_configured: "voice_model_not_configured",
    decoder_unavailable: "decoder_unavailable",
    unsupported_audio_type: "unsupported_audio_type",
    empty_audio: "empty_audio",
    audio_too_large: "audio_too_large",
    audio_decode_timeout: "audio_decode_timeout",
    audio_decode_failed: "audio_decode_failed",
    audio_too_long: "audio_too_long",
    no_speech_detected: "no_speech_detected",
    voice_turn_failed: "voice_turn_failed",
    voice_turn_timeout: "voice_turn_timeout"
  };
  var BACKEND_SPEECH_ERRORS = {
    browser_voice_unavailable: "browser_voice_unavailable",
    voice_model_not_configured: "voice_model_not_configured",
    speech_busy: "speech_busy",
    invalid_speech_text: "speech_failed",
    speech_failed: "speech_failed"
  };
  var ERROR_CODES = [
    "permission_denied", "no_microphone", "microphone_unavailable", "capture_failed",
    "unsupported_browser", "empty_recording", "recording_too_large", "network_error",
    "speech_failed", "speech_busy", "playback_failed"
  ].concat(Object.keys(BACKEND_TURN_ERRORS));

  function normalizeMediaType(value) {
    if (typeof value !== "string") return "";
    return value.split(";")[0].trim().toLowerCase();
  }

  function selectRecorderMimeType(MediaRecorderCtor, acceptedTypes) {
    if (!MediaRecorderCtor || typeof MediaRecorderCtor.isTypeSupported !== "function") return null;
    var accepted = acceptedTypes || DEFAULT_ACCEPTED_TYPES;
    for (var i = 0; i < RECORDER_CANDIDATES.length; i++) {
      var candidate = RECORDER_CANDIDATES[i];
      if (accepted.indexOf(normalizeMediaType(candidate)) === -1) continue;
      var ok = false;
      try { ok = !!MediaRecorderCtor.isTypeSupported(candidate); } catch (e) { ok = false; }
      if (ok) return candidate;
    }
    return null;
  }

  // Pure feature detection: reads injected references only, never calls them.
  function detectFeatures(deps) {
    var d = deps || {};
    var mimeType = selectRecorderMimeType(d.MediaRecorder, d.acceptedMediaTypes);
    var supported = !!(
      d.mediaDevices && typeof d.mediaDevices.getUserMedia === "function" &&
      typeof d.MediaRecorder === "function" && typeof d.AudioContext === "function" &&
      typeof d.fetch === "function" && typeof d.Blob === "function" && mimeType
    );
    return { supported: supported, mimeType: mimeType };
  }

  function classifyMediaError(err) {
    var name = err && err.name;
    if (name === "NotAllowedError" || name === "SecurityError" || name === "PermissionDeniedError") return "permission_denied";
    if (name === "NotFoundError" || name === "OverconstrainedError" || name === "DevicesNotFoundError") return "no_microphone";
    if (name === "NotReadableError" || name === "AbortError" || name === "TrackStartError") return "microphone_unavailable";
    return "capture_failed";
  }

  function createBrowserVoiceController(deps) {
    var d = deps || {};
    var maxBytes = d.maxUploadBytes || MAX_UPLOAD_BYTES;
    var maxSeconds = d.maxDurationSeconds || MAX_DURATION_SECONDS;
    var accepted = d.acceptedMediaTypes || DEFAULT_ACCEPTED_TYPES;
    var onUpdate = typeof d.onUpdate === "function" ? d.onUpdate : function () {};

    var sequence = 0;
    var disposed = false;
    var speakOverride = null;
    var phase = "idle", error = null, notice = null, level = null;
    var transcript = "", response = "", turnId = null;
    var capture = null; // active recording resources
    var turn = null;    // in-flight network request {kind, id, ac}
    var audio = null;   // {url, el}

    function apiBase() { return String(d.apiBase || "").replace(/\/$/, ""); }
    function isStale(seq) { return disposed || seq !== sequence; }
    function wantsSpeak() {
      if (speakOverride !== null) return speakOverride;
      return typeof d.speak === "function" ? !!d.speak() : !!d.speak;
    }

    function snapshot() {
      var idleish = phase === "idle" || phase === "error";
      return {
        phase: phase, error: error, notice: notice, level: level,
        transcript: transcript, response: response, turnId: turnId,
        busy: !!BUSY_PHASES[phase],
        recording: phase === "recording",
        playing: phase === "playing",
        canPlay: !!audio && idleish
      };
    }
    function emit() { if (!disposed) onUpdate(snapshot()); }

    function setError(code) {
      turn = null; level = null;
      phase = "error"; error = code; notice = null;
      emit();
    }
    function backToIdle() { turn = null; level = null; phase = "idle"; emit(); }

    function stopStream(stream) {
      if (!stream || typeof stream.getTracks !== "function") return;
      var tracks = stream.getTracks();
      for (var i = 0; i < tracks.length; i++) { try { tracks[i].stop(); } catch (e) { /* ignore */ } }
    }

    function stopMeterAndTimer(cap) {
      if (cap.timer !== null) { d.clearTimeout(cap.timer); cap.timer = null; }
      if (cap.raf !== null) { d.cancelAnimationFrame(cap.raf); cap.raf = null; }
    }

    // Idempotent: tracks, context, timers, and handlers are each released exactly once.
    function releaseCapture() {
      var cap = capture;
      if (!cap) return;
      capture = null;
      stopMeterAndTimer(cap);
      var rec = cap.recorder;
      if (rec) {
        rec.ondataavailable = null; rec.onstop = null; rec.onerror = null;
        try { if (rec.state && rec.state !== "inactive") rec.stop(); } catch (e) { /* ignore */ }
      }
      if (cap.source && typeof cap.source.disconnect === "function") { try { cap.source.disconnect(); } catch (e) { /* ignore */ } }
      stopStream(cap.stream);
      if (cap.ctx && typeof cap.ctx.close === "function") {
        try { var p = cap.ctx.close(); if (p && typeof p.catch === "function") p.catch(function () {}); } catch (e) { /* ignore */ }
      }
    }

    function releaseAudio() {
      var a = audio;
      if (!a) return;
      audio = null;
      if (a.el) {
        a.el.onended = null; a.el.onerror = null;
        try { a.el.pause(); } catch (e) { /* ignore */ }
        try { a.el.currentTime = 0; } catch (e) { /* ignore */ }
      }
      try { d.URL.revokeObjectURL(a.url); } catch (e) { /* ignore */ }
    }

    function failLocal(code) {
      sequence++;
      releaseCapture();
      setError(code);
    }

    function sendCancel(id) {
      var p;
      try {
        p = d.fetch(apiBase() + "/voice-cancel", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ turn_id: id }), cache: "no-store", credentials: "omit", keepalive: true
        });
      } catch (e) { return Promise.resolve(); }
      return Promise.resolve(p).then(function () {}, function () {});
    }

    function newAbort() {
      var Ctor = d.AbortController || (typeof AbortController !== "undefined" ? AbortController : null);
      return Ctor ? new Ctor() : { signal: undefined, abort: function () {} };
    }

    function meterLoop(cap) {
      var buf = new Float32Array(cap.analyser.fftSize || 1024);
      var tick = function () {
        if (capture !== cap || cap.raf === null) return;
        var sum = 0, n = buf.length;
        cap.analyser.getFloatTimeDomainData(buf);
        for (var i = 0; i < n; i++) sum += buf[i] * buf[i];
        var rms = Math.sqrt(sum / n);
        level = Math.max(0, Math.min(1, rms * 3));
        emit();
        cap.raf = d.requestAnimationFrame(tick);
      };
      cap.raf = d.requestAnimationFrame(tick);
    }

    async function start() {
      if (disposed || (phase !== "idle" && phase !== "error")) return false;
      releaseAudio();
      transcript = ""; response = ""; error = null; notice = null; level = null;
      var features = detectFeatures(d);
      if (!features.supported || typeof d.randomUUID !== "function") {
        turnId = null;
        setError("unsupported_browser");
        return false;
      }
      var seq = ++sequence;
      turnId = d.randomUUID();
      phase = "requesting";
      emit();
      var stream;
      try {
        stream = await d.mediaDevices.getUserMedia({ audio: true });
      } catch (e) {
        if (isStale(seq)) return false;
        setError(classifyMediaError(e));
        return false;
      }
      if (isStale(seq)) { stopStream(stream); return false; }

      var cap = { seq: seq, id: turnId, stream: stream, ctx: null, source: null, analyser: null,
        recorder: null, chunks: [], bytes: 0, timer: null, raf: null, mimeType: features.mimeType, stopping: false };
      capture = cap;
      try {
        cap.ctx = new d.AudioContext();
        cap.source = cap.ctx.createMediaStreamSource(stream);
        cap.analyser = cap.ctx.createAnalyser();
        cap.analyser.fftSize = 1024;
        cap.source.connect(cap.analyser);
        var rec = new d.MediaRecorder(stream, { mimeType: features.mimeType });
        cap.recorder = rec;
        rec.ondataavailable = function (ev) {
          if (capture !== cap || !ev || !ev.data || !(ev.data.size > 0)) return;
          cap.chunks.push(ev.data);
          cap.bytes += ev.data.size;
          if (cap.bytes > maxBytes) failLocal("recording_too_large");
        };
        rec.onstop = function () { if (capture === cap) finalize(cap); };
        rec.onerror = function () { if (capture === cap) failLocal("capture_failed"); };
        rec.start(1000);
        cap.timer = d.setTimeout(function () { cap.timer = null; stopAndSend(); }, maxSeconds * 1000);
        meterLoop(cap);
      } catch (e) {
        failLocal("capture_failed");
        return false;
      }
      phase = "recording";
      emit();
      return true;
    }

    function stopAndSend() {
      var cap = capture;
      if (disposed || phase !== "recording" || !cap || cap.stopping) return false;
      cap.stopping = true;
      stopMeterAndTimer(cap);
      phase = "processing";
      level = null;
      emit();
      try { cap.recorder.stop(); } catch (e) { failLocal("capture_failed"); return false; }
      return true;
    }

    function finalize(cap) {
      if (isStale(cap.seq)) { releaseCapture(); return; }
      var rec = cap.recorder;
      var recType = (rec && typeof rec.mimeType === "string" && rec.mimeType) ? rec.mimeType : cap.mimeType;
      var chunks = cap.chunks;
      var id = cap.id;
      var seq = cap.seq;
      releaseCapture();
      if (accepted.indexOf(normalizeMediaType(recType)) === -1) { failLocal("unsupported_browser"); return; }
      var blob;
      try { blob = new d.Blob(chunks, { type: recType }); } catch (e) { failLocal("capture_failed"); return; }
      if (!(blob.size > 0)) { failLocal("empty_recording"); return; }
      if (blob.size > maxBytes) { failLocal("recording_too_large"); return; }
      upload(seq, id, blob, recType);
    }

    async function upload(seq, id, blob, type) {
      var ac = newAbort();
      turn = { kind: "voice", id: id, ac: ac };
      var res, body = null;
      try {
        res = await d.fetch(apiBase() + "/voice-turn?turn_id=" + encodeURIComponent(id), {
          method: "POST", headers: { "Content-Type": type }, body: blob,
          signal: ac.signal, cache: "no-store", credentials: "omit"
        });
        if (isStale(seq)) return;
        try { body = await res.json(); } catch (e) { body = null; }
      } catch (e) {
        if (isStale(seq)) return;
        setError("network_error");
        return;
      }
      if (isStale(seq)) return;
      if (!res.ok) {
        var code = body && typeof body === "object" ? BACKEND_TURN_ERRORS[body.error] : undefined;
        setError(code || "voice_turn_failed");
        return;
      }
      if (!body || typeof body !== "object" || body.turn_id !== id ||
          typeof body.transcript !== "string" || typeof body.response !== "string") {
        setError("voice_turn_failed");
        return;
      }
      transcript = body.transcript;
      response = body.response;
      turn = null;
      phase = "idle";
      emit(); // text is delivered before any synthesis
      if (wantsSpeak() && response) await synthesize(seq, response);
    }

    async function synthesize(seq, text) {
      var ac = newAbort();
      turn = { kind: "speech", id: turnId, ac: ac };
      phase = "synthesizing";
      emit();
      var res, buf, body = null;
      try {
        res = await d.fetch(apiBase() + "/voice-speech", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: text }), signal: ac.signal, cache: "no-store", credentials: "omit"
        });
        if (isStale(seq)) return;
        if (res.ok) buf = await res.arrayBuffer();
        else { try { body = await res.json(); } catch (e) { body = null; } }
      } catch (e) {
        if (isStale(seq)) return;
        setError("speech_failed");
        return;
      }
      if (isStale(seq)) return;
      if (!res.ok) {
        var code = body && typeof body === "object" ? BACKEND_SPEECH_ERRORS[body.error] : undefined;
        setError(code || "speech_failed");
        return;
      }
      var url;
      try {
        url = d.URL.createObjectURL(new d.Blob([buf], { type: "audio/wav" }));
      } catch (e) {
        setError("speech_failed");
        return;
      }
      releaseAudio();
      audio = { url: url, el: null };
      turn = null;
      await playAudio(seq);
    }

    async function playAudio(seq) {
      var a = audio;
      if (!a) return;
      var el = a.el;
      if (!el) {
        el = new d.Audio(a.url);
        a.el = el;
        el.onended = function () {
          if (audio === a && phase === "playing") { sequence++; phase = "idle"; emit(); }
        };
        el.onerror = function () {
          if (audio === a && phase === "playing") { sequence++; setError("playback_failed"); }
        };
      } else {
        try { el.currentTime = 0; } catch (e) { /* ignore */ }
      }
      error = null; notice = null;
      phase = "playing";
      emit();
      try {
        await el.play();
      } catch (e) {
        if (isStale(seq) || audio !== a) return;
        sequence++;
        phase = "idle";
        notice = "autoplay_blocked";
        emit();
      }
    }

    function play() {
      if (disposed || !audio || (phase !== "idle" && phase !== "error")) return false;
      playAudio(++sequence);
      return true;
    }

    function stopPlayback() {
      if (disposed || phase !== "playing" || !audio) return false;
      sequence++;
      var el = audio.el;
      if (el) {
        try { el.pause(); } catch (e) { /* ignore */ }
        try { el.currentTime = 0; } catch (e) { /* ignore */ }
      }
      phase = "idle";
      emit();
      return true;
    }

    async function cancel() {
      if (disposed) return false;
      if (phase === "requesting" || phase === "recording" || (phase === "processing" && !turn)) {
        sequence++;
        releaseCapture();
        transcript = ""; response = ""; turnId = null; error = null; notice = null;
        backToIdle();
        return true;
      }
      if (phase === "processing" && turn && turn.kind === "voice") {
        var t = turn;
        sequence++;
        transcript = ""; response = ""; error = null; notice = null;
        try { t.ac.abort(); } catch (e) { /* ignore */ }
        backToIdle();
        await sendCancel(t.id);
        return true;
      }
      if (phase === "synthesizing" && turn) {
        var s = turn;
        sequence++;
        try { s.ac.abort(); } catch (e) { /* ignore */ }
        backToIdle();
        return true;
      }
      if (phase === "playing") {
        sequence++;
        releaseAudio();
        backToIdle();
        return true;
      }
      return false;
    }

    function dispose() {
      if (disposed) return;
      var t = turn;
      disposed = true;
      sequence++;
      turn = null;
      releaseCapture();
      releaseAudio();
      if (t) {
        try { t.ac.abort(); } catch (e) { /* ignore */ }
        if (t.kind === "voice") sendCancel(t.id);
      }
    }

    function setSpeak(value) { speakOverride = !!value; }

    return {
      start: start, stopAndSend: stopAndSend, cancel: cancel, play: play,
      stopPlayback: stopPlayback, setSpeak: setSpeak, dispose: dispose, getSnapshot: snapshot
    };
  }

  var api = {
    MAX_UPLOAD_BYTES: MAX_UPLOAD_BYTES,
    MAX_DURATION_SECONDS: MAX_DURATION_SECONDS,
    DEFAULT_ACCEPTED_TYPES: DEFAULT_ACCEPTED_TYPES,
    ERROR_CODES: ERROR_CODES,
    normalizeMediaType: normalizeMediaType,
    selectRecorderMimeType: selectRecorderMimeType,
    detectFeatures: detectFeatures,
    classifyMediaError: classifyMediaError,
    createBrowserVoiceController: createBrowserVoiceController
  };

  if (typeof window !== "undefined") window.JarvisVoice = api;
  if (typeof module === "object" && module && module.exports) module.exports = api;
})();
