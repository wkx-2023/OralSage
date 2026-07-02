from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import whisper
from pydub import AudioSegment
from starlette.concurrency import run_in_threadpool

from backend.services.env_loader import load_project_env

load_project_env()

logger = logging.getLogger("voice_service")

class VoiceService:
    _model = None

    @classmethod
    def get_model(cls):
        if cls._model is None:
            model_size = os.getenv("WHISPER_MODEL", "base")
            logger.info(f"Loading Whisper model: {model_size}")
            # This will download the model on first run
            cls._model = whisper.load_model(model_size)
        return cls._model

    @classmethod
    def transcribe(cls, audio_path: str | Path) -> str:
        """
        Transcribe an audio file to text using OpenAI Whisper.
        Supports various formats via pydub/ffmpeg.
        """
        model = cls.get_model()
        
        # Ensure path is Path object
        audio_path = Path(audio_path)
        
        # If not wav, convert to wav for better compatibility with whisper
        # Actually whisper supports many formats via ffmpeg, but we use pydub for safety
        temp_wav = None
        try:
            if audio_path.suffix.lower() != ".wav":
                logger.info(f"Converting {audio_path.suffix} to wav...")
                audio = AudioSegment.from_file(str(audio_path))
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    audio.export(tmp.name, format="wav")
                    temp_wav = tmp.name
                input_path = temp_wav
            else:
                input_path = str(audio_path)

            logger.info(f"Transcribing {input_path}...")
            result = model.transcribe(input_path, language="zh")
            return result.get("text", "").strip()

        finally:
            if temp_wav and os.path.exists(temp_wav):
                os.remove(temp_wav)

async def run_transcription(audio_content: bytes, filename: str) -> str:
    """
    High-level helper for API endpoints.
    """
    with tempfile.NamedTemporaryFile(suffix=Path(filename).suffix, delete=False) as tmp:
        tmp.write(audio_content)
        tmp_path = tmp.name

    try:
        # Using run_in_threadpool to avoid blocking the FastAPI event loop
        return await run_in_threadpool(VoiceService.transcribe, tmp_path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
