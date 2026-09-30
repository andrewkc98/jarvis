"""Deterministic source-contract tests for the HUD.

These tests read the production ``hud/index.html`` and assert its wiring
without opening a browser or binding a port. Nothing here imports a browser,
binds a socket, or spawns a server: the assertions are pure text over the
authoritative source plus a ``node --check`` syntax pass on the inline script.
"""

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

HUD_HTML_PATH = Path(__file__).resolve().parents[1] / "hud" / "index.html"

STALE_DEPLOY_ERROR = (
    "HUD still routes the production Approve button to decide(\"approve\"). "
    "The implemented API route is /allow and the button must call decide(\"allow\")."
)


def _extract_inline_script(html: str) -> str:
    """Return the single inline <script> body.

    The page must contain exactly two scripts: the external ``voice.js`` (loaded
    first) and exactly one inline application script.
    """

    tags = re.findall(r"<script\b[^>]*>", html)
    assert len(tags) == 2, HUD_HTML_PATH
    assert tags[0] == '<script src="voice.js">', tags
    assert tags[1] == "<script>", tags
    assert html.index('<script src="voice.js">') < html.index("<script>\n")
    match = re.search(r"<script>(.*?)</script>", html, re.DOTALL)
    assert match is not None, "no inline <script> block found"
    return match.group(1)


def test_approve_listener_targets_allowed_route():
    """The unique production Approve button click must call decide("allow"),
    matching the implemented POST /allow route — never decide("approve")."""

    html = HUD_HTML_PATH.read_text()
    script = _extract_inline_script(html)

    approve_match = re.search(
        r'(?m)\$\("approveBtn"\)\s*\.\s*addEventListener\s*\(\s*"click"\s*,\s*'
        r"\(\)\s*=>\s*decide\(\s*\"allow\"\s*\)\s*\)\s*;",
        script,
    )
    assert approve_match is not None, STALE_DEPLOY_ERROR
    # Exactly one Approve listener — not duplicated or wired any other way.
    assert re.findall(r'\$\("approveBtn"\)\s*\.\s*addEventListener\s*\(', script) == [
        '$("approveBtn").addEventListener('
    ]
    assert approve_match.group(0) == '$("approveBtn").addEventListener("click", () => decide("allow"));'


def test_deny_listener_targets_denied_route():
    """Deny remains bound to decide("deny"), matching POST /deny."""

    html = HUD_HTML_PATH.read_text()
    script = _extract_inline_script(html)

    deny_match = re.search(
        r'(?m)\$\("denyBtn"\)\s*\.\s*addEventListener\s*\(\s*"click"\s*,\s*'
        r"\(\)\s*=>\s*decide\(\s*\"deny\"\s*\)\s*\)\s*;",
        script,
    )
    assert deny_match is not None, "unique Approve and Deny listeners"
    assert deny_match.group(0) == '$("denyBtn").addEventListener("click", () => decide("deny"));'
    assert re.findall(r'\$\("denyBtn"\)\s*\.\s*addEventListener\s*\(', script) == [
        '$("denyBtn").addEventListener('
    ]


def test_no_stale_approval_wiring():
    """The stale decide("approve") wiring must be gone — it has no route."""

    html = HUD_HTML_PATH.read_text()
    script = _extract_inline_script(html)

    assert 'decide("approve")' not in script
    # Only the two authoritative decision verbs remain as button click handlers.
    assert sorted(re.findall(r'decide\(\s*\"(allow|deny)\"\s*\)', script)) == ["allow", "deny"]


def test_idle_note_is_truthful_and_stale_binding_removed():
    """The audio idle note must be the exact truthful label and must not
    reference "fn" or any held key. The stale CONFIG.pttBinding source must be
    gone too (no "hold"/"fn"/"'enter'/ptt")."""

    html = HUD_HTML_PATH.read_text()
    script = _extract_inline_script(html)

    # The exact production label the audio panel renders.
    expected = "push-to-talk · Enter in terminal to start/stop"
    assert expected in script, "idle audio note is stale or missing"
    assert "NOTES[" in script and "NOTES.idle" in script, "audio note rendering must still use NOTES.idle"
    assert "audioNote" in script and "$(" in script and ".textContent" in script

    # Stale push-to-talk source: no held modifier, no ptt config key.
    for forbidden in ("hold ", '"fn"', "'fn'", "pttBinding", "ptt"):
        assert forbidden not in script, f"stale '{forbidden}' must be removed"
    assert "'enter'" not in script, "stale enter-pressed binding removed"


def test_api_routes_and_request_construction_untouched():
    """POST /allow and /deny routes still build request bodies from an `id`,
    preserving request construction and approval adaptation behavior."""

    html = HUD_HTML_PATH.read_text()
    script = _extract_inline_script(html)

    # Request construction is preserved: approve/deny build a JSON body carrying
    # the pending approval's `id` and POST with the application/json Content-Type.
    assert "method: \"POST\"" in script
    assert "Content-Type\": \"application/json\"" in script
    assert "JSON.stringify({ id: a.id })" in script
    # Approval busy/error/busy-clear adaptation still drives request outcomes.
    assert "approvalBusy" in script
    assert "approvalErr" in script
    assert "down" in script and "err" in script


def test_javascript_syntax_valid():
    """The full inline script passes a real JavaScript parser via node --check.

    The check runs outside the repository root. If ``node`` is unavailable in
    this environment the syntax gate is skipped rather than failing the suite;
    the CI verification uses the same check and requires it to pass."""

    if shutil.which("node") is None:
        pytest.skip("node not available")

    html = HUD_HTML_PATH.read_text()
    script = _extract_inline_script(html)

    fd, path = tempfile.mkstemp(
        prefix="hud-inline-", suffix=".js", dir=tempfile.gettempdir()
    )
    try:
        os.close(fd)
        with open(path, "w") as handle:
            handle.write(script)
        completed = subprocess.run(
            ["node", "--check", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert completed.returncode == 0, (
            f"inline HUD script failed node --check:\n{completed.stdout}"
        )
    finally:
        os.unlink(path)


# ---------------------------------------------------------------------------
# Browser voice integration contract
# ---------------------------------------------------------------------------

VOICE_JS_PATH = HUD_HTML_PATH.parent / "voice.js"

VOICE_DOM_IDS = (
    "voiceStartBtn",
    "voiceStopBtn",
    "voiceCancelBtn",
    "voicePlayBtn",
    "voiceStopPlayBtn",
    "voiceStatus",
    "voiceTranscriptBar",
    "voiceTranscript",
    "voiceReplyBar",
    "voiceReply",
)


def _html_and_script():
    html = HUD_HTML_PATH.read_text()
    return html, _extract_inline_script(html)


def test_voice_script_loaded_before_inline_and_syntax_checked():
    html, script = _html_and_script()
    assert html.count('src="voice.js"') == 1
    if shutil.which("node") is None:
        pytest.skip("node not available")
    completed = subprocess.run(
        ["node", "--check", str(VOICE_JS_PATH)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout


def test_voice_controls_exist_and_are_accessible():
    html, script = _html_and_script()
    for dom_id in VOICE_DOM_IDS:
        assert html.count(f'id="{dom_id}"') == 1, dom_id
        assert f'$("{dom_id}")' in script, dom_id
    for button in ("voiceStartBtn", "voiceStopBtn", "voiceCancelBtn", "voicePlayBtn", "voiceStopPlayBtn"):
        tag = re.search(rf'<button[^>]*id="{button}"[^>]*>', html)
        assert tag is not None, button
        assert 'type="button"' in tag.group(0)
        assert "aria-label=" in tag.group(0)
    assert 'id="voiceStatus" role="status" aria-live="polite"' in html
    # Approval and typed-command layouts are untouched.
    for dom_id in ("approveBtn", "denyBtn", "cmdInput", "speakToggle", "cmdReplyBar", "eventsList", "vaultOk", "turnsList"):
        assert html.count(f'id="{dom_id}"') == 1, dom_id


def test_voice_listeners_and_controller_wiring():
    html, script = _html_and_script()
    wiring = {
        "voiceStartBtn": "voiceController.start()",
        "voiceStopBtn": "voiceController.stopAndSend()",
        "voiceCancelBtn": "voiceController.cancel()",
        "voicePlayBtn": "voiceController.play()",
        "voiceStopPlayBtn": "voiceController.stopPlayback()",
    }
    for dom_id, call in wiring.items():
        assert len(re.findall(rf'\$\("{dom_id}"\)\.addEventListener\("click"', script)) == 1, dom_id
        block = re.search(rf'\$\("{dom_id}"\)\.addEventListener\("click".*?\n?\}}?\);', script, re.DOTALL)
        assert call in script[block.start():block.start() + 400], dom_id
    assert 'window.addEventListener("pagehide", disposeVoice);' in script
    assert 'window.addEventListener("unload", disposeVoice);' in script
    assert "voiceController.dispose()" in script
    # Controller only for an available browser-mode capability, fed through setState.
    assert 'caps.mode === "browser" && caps.available' in script
    assert "createBrowserVoiceController(deps)" in script
    assert "detectFeatures(deps).supported" in script
    assert "onUpdate: (snapshot) => setState({ voice: snapshot })" in script
    assert "speak: () => $(\"speakToggle\").checked" in script
    assert "apiBase: base()" in script


def test_capabilities_fetch_is_get_only_and_never_requests_permission():
    html, script = _html_and_script()
    assert 'req("/voice-capabilities", undefined, 3000)' in script
    assert "/voice-turn" not in script and "/voice-speech" not in script and "/voice-cancel" not in script
    # getUserMedia belongs to the controller, triggered only by the Start click.
    assert "getUserMedia" not in script
    assert "mediaDevices: navigator.mediaDevices" in script
    # file:// mock mode never fetches capabilities or builds a controller.
    assert re.search(r'location\.protocol === "file:"\)\s*\{\s*setState\(\{ voiceCapsState: "mock" \}\);\s*return;', script)
    mock_pos = script.index('location.protocol === "file:"')
    assert mock_pos < script.index('req("/voice-capabilities"')
    assert mock_pos < script.index("createBrowserVoiceController(deps)")
    # Exact capability contract keys.
    assert '"accepted_media_types,available,max_duration_seconds,max_upload_bytes,mode,reason"' in script


def test_text_content_only_for_voice_content_and_no_forbidden_apis():
    html, script = _html_and_script()
    for dom_id in ("voiceTranscript", "voiceReply", "voiceStatus"):
        assert f'$("{dom_id}").textContent =' in script, dom_id
        assert f'$("{dom_id}").innerHTML' not in script, dom_id
    voice_blocks = script[script.index("// voice-view:start"):script.index("// voice-view:end")]
    assert "innerHTML" not in voice_blocks
    voice_js = VOICE_JS_PATH.read_text()
    for source in (script, voice_js):
        for forbidden in (
            "SpeechRecognition",
            "webkitSpeechRecognition",
            "speechSynthesis",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "document.cookie",
        ):
            assert forbidden not in source, forbidden
    assert "innerHTML" not in voice_js


def test_typed_command_contract_preserved():
    html, script = _html_and_script()
    assert 'req("/command", {' in script
    assert "JSON.stringify({ text, speak })" in script
    assert 'const speak = $("speakToggle").checked;' in script
    assert 'if (e.key === "Enter") { e.preventDefault(); submitCmd(); }' in script
    assert 'e.metaKey && e.shiftKey && (e.key === "h" || e.key === "H")' in script
    assert "if (!text || STATE.cmdBusy || (STATE.voice && STATE.voice.busy)) return;" in script
    # Truthful terminal-mode idle note is retained; Enter-in-terminal note is only the fallback.
    assert "push-to-talk · Enter in terminal to start/stop" in script
    assert 'idleNote: mode === "browser"' in script


def _run_voice_view(tmp_path, cases):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    html, script = _html_and_script()
    start = script.index("// voice-view:start")
    end = script.index("// voice-view:end")
    program = (
        script[start:end]
        + "\nconst fs = require('fs');\n"
        + "const cases = JSON.parse(fs.readFileSync(0, 'utf8'));\n"
        + "process.stdout.write(JSON.stringify(cases.map(({st, env}) => computeVoiceView(st, env))));\n"
    )
    path = tmp_path / "voice_view.js"
    path.write_text(program, encoding="utf-8")
    import json

    completed = subprocess.run(
        [node, str(path)],
        input=json.dumps(cases),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _caps(mode, available=True, reason=None):
    return {
        "mode": mode,
        "available": available,
        "reason": reason,
        "max_duration_seconds": 30,
        "max_upload_bytes": 8388608,
        "accepted_media_types": ["audio/webm", "audio/ogg", "audio/mp4"],
    }


def _voice(phase="idle", **over):
    snap = {
        "phase": phase,
        "error": None,
        "notice": None,
        "level": None,
        "transcript": "",
        "response": "",
        "turnId": None,
        "busy": phase in ("requesting", "recording", "processing", "synthesizing", "playing"),
        "recording": phase == "recording",
        "playing": phase == "playing",
        "canPlay": False,
    }
    snap.update(over)
    return snap


def _state(**over):
    st = {
        "voiceCaps": _caps("browser"),
        "voiceCapsState": "ready",
        "voice": None,
        "cmdBusy": False,
        "cmdReply": None,
        "redacted": False,
    }
    st.update(over)
    return st


def test_capability_fallback_guidance(tmp_path):
    env_none = {"controller": False, "approvalPending": False}
    cases = [
        {"st": _state(voiceCaps=_caps("terminal", False, "terminal_voice_active")), "env": env_none},
        {"st": _state(voiceCaps=_caps("off", False, "voice_disabled")), "env": env_none},
        {"st": _state(voiceCaps=_caps("browser", False, "voice_model_not_configured")), "env": env_none},
        {"st": _state(voiceCaps=_caps("browser", False, "decoder_unavailable")), "env": env_none},
        {"st": _state(), "env": env_none},  # available but browser lacks features
        {"st": _state(voiceCaps=None, voiceCapsState="failed"), "env": env_none},
        {"st": _state(voiceCaps=None, voiceCapsState="mock"), "env": env_none},
        {"st": _state(voiceCaps=None, voiceCapsState="pending"), "env": env_none},
        {"st": _state(), "env": {"controller": True, "approvalPending": False}},
    ]
    out = _run_voice_view(tmp_path, cases)
    assert "jarvis-hud --browser-voice" in out[0]["status"]
    assert out[0]["startDisabled"] is True
    assert "disabled" in out[1]["status"] and "Typed commands" in out[1]["status"]
    assert "Voice model is not configured" in out[2]["status"] and "Typed commands" in out[2]["status"]
    assert "ffmpeg" in out[3]["status"] and "Typed commands" in out[3]["status"]
    assert "cannot record" in out[4]["status"] and "Typed commands" in out[4]["status"]
    assert "Voice status unavailable" in out[5]["status"]
    assert "file://" in out[6]["status"]
    assert out[7]["status"] == ""
    for view in out[:8]:
        assert view["startDisabled"] is True
    assert out[8]["startDisabled"] is False
    assert out[8]["idleNote"] == "browser mic · Start mic, then Stop & Send"
    assert out[1]["idleNote"] == "voice input off · typed commands only"
    assert out[0]["idleNote"] is None  # terminal mode keeps the Enter-in-terminal note


def test_state_precedence_and_disabling(tmp_path):
    env = {"controller": True, "approvalPending": False}
    cases = [
        {"st": _state(voice=_voice("requesting")), "env": env},
        {"st": _state(voice=_voice("recording", level=0.4)), "env": env},
        {"st": _state(voice=_voice("processing")), "env": env},
        {"st": _state(voice=_voice("synthesizing", transcript="hi", response="yo")), "env": env},
        {"st": _state(voice=_voice("playing", transcript="hi", response="yo", canPlay=False)), "env": env},
        {"st": _state(voice=_voice("error", error="permission_denied")), "env": env},
        {"st": _state(voice=_voice("idle", transcript="hi", response="yo", canPlay=True)), "env": env},
        {"st": _state(voice=_voice("idle", notice="autoplay_blocked", transcript="a", response="b", canPlay=True)), "env": env},
        {"st": _state(), "env": {"controller": True, "approvalPending": True}},
        {"st": _state(cmdBusy=True), "env": env},
        {"st": _state(voice=_voice("recording")), "env": {"controller": True, "approvalPending": True}},
    ]
    out = _run_voice_view(tmp_path, cases)
    req_, rec, proc, syn, play, err, idle_reply, autoplay, appr, cmdbusy, rec_appr = out
    assert req_["audioKey"] == "idle" and req_["localOwns"] and req_["typedDisabled"] and req_["speakDisabled"]
    assert rec["audioKey"] == "listening" and rec["levelOverride"] and rec["level"] == 0.4
    assert rec["stopDisabled"] is False and rec["cancelDisabled"] is False and rec["startDisabled"] is True
    assert proc["audioKey"] == "processing" and proc["stopDisabled"] is True and proc["cancelDisabled"] is False
    assert syn["audioKey"] == "processing" and syn["typedDisabled"] and syn["replyHidden"] is False
    assert play["audioKey"] == "speaking" and play["stopPlayHidden"] is False and play["cancelDisabled"] is False
    assert err["audioKey"] == "error" and err["levelOverride"] and "site settings" in err["status"]
    assert err["typedDisabled"] is False and err["startDisabled"] is False
    # Idle controller: backend runtime status stays authoritative, reply can be replayed.
    assert idle_reply["audioKey"] is None and idle_reply["levelOverride"] is False
    assert idle_reply["playHidden"] is False and idle_reply["typedDisabled"] is False
    assert "Play reply" in autoplay["status"] and autoplay["playHidden"] is False
    assert appr["startDisabled"] is True
    assert cmdbusy["startDisabled"] is True and cmdbusy["typedDisabled"] is True
    # An approval appearing mid-recording keeps Cancel and Stop operable.
    assert rec_appr["cancelDisabled"] is False and rec_appr["stopDisabled"] is False


def test_redaction_hides_typed_and_voice_replies_without_losing_state(tmp_path):
    env = {"controller": True, "approvalPending": False}
    voice = _voice("idle", transcript="secret words", response="secret reply", canPlay=True)
    playing = _voice("playing", transcript="secret words", response="secret reply", canPlay=False)
    cases = [
        {"st": _state(voice=voice, cmdReply="typed reply", redacted=False), "env": env},
        {"st": _state(voice=voice, cmdReply="typed reply", redacted=True), "env": env},
        {"st": _state(voice=playing, cmdReply="typed reply", redacted=True), "env": env},
        {"st": _state(voice=voice, cmdReply="typed reply", redacted=False), "env": env},
        {"st": _state(voice=None, cmdReply=None, redacted=False), "env": env},
    ]
    shown, hidden, hidden_playing, restored, empty = _run_voice_view(tmp_path, cases)
    assert shown["cmdReplyHidden"] is False
    assert shown["transcriptHidden"] is False and shown["replyHidden"] is False
    assert shown["playHidden"] is False
    assert hidden["cmdReplyHidden"] is True
    assert hidden["transcriptHidden"] is True and hidden["replyHidden"] is True
    assert hidden["playHidden"] is True
    assert hidden_playing["stopPlayHidden"] is True
    # State is retained in the snapshot itself; only presentation flips back.
    assert hidden["transcript"] == "secret words" and hidden["response"] == "secret reply"
    assert restored == shown
    assert empty["cmdReplyHidden"] is True and empty["transcriptHidden"] is True and empty["replyHidden"] is True


def test_render_applies_redaction_to_all_reply_surfaces():
    html, script = _html_and_script()
    assert '$("voiceTranscriptBar").hidden = vals.voice.transcriptHidden;' in script
    assert '$("voiceReplyBar").hidden = vals.voice.replyHidden;' in script
    assert '$("cmdReplyBar").hidden = !vals.cmdReplyVisible;' in script
    assert 'cmdReplyVisible: !vv.cmdReplyHidden' in script
    assert '$("voicePlayBtn").hidden = vv.playHidden;' in script
    assert '$("voiceStopPlayBtn").hidden = vv.stopPlayHidden;' in script
    assert 'cmdInput.disabled = vals.cmdBusy;' in script
    assert '$("speakToggle").disabled = vals.cmdBusy;' in script
    assert "cmdBusy: vv.typedDisabled" in script
    # Poll/runtime status cannot override local voice state while the controller owns a turn.
    assert "const aKey = vv.audioKey || (st.runtimeValid" in script
    assert "const lvl = vv.levelOverride ? vv.level : st.level;" in script
