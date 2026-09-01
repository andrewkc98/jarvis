"""Application service connecting audio capture, transcription, and speech."""

from jarvis.voice import capture, stt, tts


class JarvisService:
    """Run one turn of Jarvis using a configured Piper voice model."""

    def __init__(self, voice_model_path) -> None:
        self.voice_model_path = voice_model_path

    def _speak_response(self, text: str) -> str:
        response = f"You said: {text}"
        tts.speak(response, model_path=self.voice_model_path)
        return response

    def run_once(self) -> str:
        """Capture and transcribe one utterance, then speak the response."""
        audio = capture.record_on_enter()
        transcript = stt.transcribe(audio)
        return self._speak_response(transcript)

    def run_text(self, text: str) -> str:
        """Speak a supplied text response without using audio capture or STT."""
        return self._speak_response(text)
