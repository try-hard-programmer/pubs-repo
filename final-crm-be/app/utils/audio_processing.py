"""
Audio processing utilities
Functions for audio upload to CDN and external transcription
"""
import httpx
import logging
from typing import Optional
from app.config import settings

logger = logging.getLogger(__name__)


async def upload_audio_to_cdn(audio_bytes: bytes, filename: str) -> Optional[str]:
    """
    Upload audio file to CDN and return the URL

    Args:
        audio_bytes: Raw audio file bytes
        filename: Original filename

    Returns:
        CDN URL of uploaded file or None if upload fails
    """
    try:
        files = {"file": (filename, audio_bytes)}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                settings.CDN_UPLOAD_URL,
                files=files
            )
            response.raise_for_status()

            # Parse response to get CDN URL
            result = response.json()
            cdn_url = result.get("fileUrl") or result.get("data", {}).get("fileUrl")

            if not cdn_url:
                logger.error(f"CDN upload response missing URL: {result}")
                return None

            logger.info(f"Successfully uploaded audio to CDN: {cdn_url}")
            return cdn_url

    except httpx.HTTPError as e:
        logger.error(f"HTTP error uploading to CDN: {e}")
        return None
    except Exception as e:
        logger.error(f"Error uploading audio to CDN: {type(e).__name__} - {e}")
        return None


async def transcribe_audio_from_url(audio_url: str) -> str:
    """
    Transcribe audio from CDN URL using external transcription API

    Args:
        audio_url: CDN URL of the audio file

    Returns:
        Transcribed text or empty string if transcription fails
    """
    try:
        # Construct transcription API endpoint
        transcription_url = f"{settings.OPENAI_BASE_URL.rstrip('/v1')}/v1/audio"

        headers = {
            "Authorization": f"Bearer {settings.TRANSCRIPTION_API_TOKEN}",
            "Content-Type": "application/json"
        }

        payload = {
            "url": audio_url,
            "model": settings.TRANSCRIPTION_MODEL
        }

        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                transcription_url,
                headers=headers,
                json=payload
            )
            response.raise_for_status()

            result = response.json()
            # Extract transcribed text from response
            # API response format: {"output": {"result": "transcribed text"}, "status": "COMPLETED"}
            text = result.get("output", {}).get("result", "")

            if not text:
                logger.error(f"No text found in transcription response: {result}")
                return ""

            logger.info(f"Successfully transcribed audio from {audio_url}")
            return text.strip()

    except httpx.HTTPError as e:
        logger.error(f"HTTP error during transcription: {e}")
        return ""
    except Exception as e:
        logger.error(f"Error transcribing audio: {type(e).__name__} - {e}")
        return ""


async def process_audio_file(audio_bytes: bytes, filename: str) -> str:
    """
    Process audio file by uploading to CDN and transcribing

    Args:
        audio_bytes: Raw audio file bytes
        filename: Original filename

    Returns:
        Transcribed text or empty string if processing fails
    """
    # Step 1: Upload to CDN
    cdn_url = await upload_audio_to_cdn(audio_bytes, filename)

    if not cdn_url:
        logger.error("Failed to upload audio to CDN")
        return ""

    # Step 2: Transcribe from CDN URL
    text = await transcribe_audio_from_url(cdn_url)

    return text
