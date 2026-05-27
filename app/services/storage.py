"""Audio object storage abstraction.

In production we use MinIO (S3-compatible). For local development and
the test suite we transparently fall back to the filesystem so the API
boots without external services.

The interface is intentionally small — ``put_object`` / ``get_object``
/ ``delete_object`` / ``presigned_get_url`` — because the rest of the
application only ever needs a stable opaque ``storage_key``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

from app.config import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StoredObject:
    """Metadata returned after a successful ``put_object`` call."""

    storage_key: str
    size_bytes: int
    content_type: str


class AudioStorage(Protocol):
    """Storage backend protocol — sync method calls, async wrapped externally."""

    async def put_object(
        self, *, key: str, data: bytes, content_type: str
    ) -> StoredObject: ...

    async def get_object(self, key: str) -> bytes: ...

    async def delete_object(self, key: str) -> None: ...

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> str: ...


class LocalAudioStorage:
    """Filesystem-backed implementation used in development and tests."""

    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir).resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, key: str) -> Path:
        # Strip any leading slashes and normalise to prevent traversal.
        clean = key.lstrip("/").replace("..", "_")
        return (self.base_dir / clean).resolve()

    async def put_object(
        self, *, key: str, data: bytes, content_type: str
    ) -> StoredObject:
        target = self._path_for(key)
        if self.base_dir not in target.parents and target != self.base_dir:
            raise ValueError("Refusing to write outside storage base dir")
        target.parent.mkdir(parents=True, exist_ok=True)

        def _write() -> None:
            target.write_bytes(data)

        await asyncio.to_thread(_write)
        return StoredObject(
            storage_key=key, size_bytes=len(data), content_type=content_type
        )

    async def get_object(self, key: str) -> bytes:
        target = self._path_for(key)

        def _read() -> bytes:
            return target.read_bytes()

        return await asyncio.to_thread(_read)

    async def delete_object(self, key: str) -> None:
        target = self._path_for(key)

        def _delete() -> None:
            try:
                os.remove(target)
            except FileNotFoundError:
                pass

        await asyncio.to_thread(_delete)

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> str:  # noqa: ARG002
        # Local backend has no real signing — return a file:// URL for
        # debugging convenience. Clients should fetch via the API.
        return f"file://{self._path_for(key)}"


class S3AudioStorage:
    """boto3-backed S3/MinIO implementation."""

    def __init__(self, settings: Settings) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - boto3 pinned
            raise RuntimeError("boto3 is required for S3 storage") from exc

        self._bucket = settings.minio_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.minio_endpoint,
            aws_access_key_id=settings.minio_access_key,
            aws_secret_access_key=settings.minio_secret_key,
            region_name=settings.minio_region,
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:  # pragma: no cover - depends on live MinIO
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except Exception as exc:
                logger.warning("Could not ensure bucket %s: %s", self._bucket, exc)

    async def put_object(
        self, *, key: str, data: bytes, content_type: str
    ) -> StoredObject:
        def _put() -> None:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=data,
                ContentType=content_type,
            )

        await asyncio.to_thread(_put)
        return StoredObject(
            storage_key=key, size_bytes=len(data), content_type=content_type
        )

    async def get_object(self, key: str) -> bytes:
        def _get() -> bytes:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()

        return await asyncio.to_thread(_get)

    async def delete_object(self, key: str) -> None:
        def _delete() -> None:
            self._client.delete_object(Bucket=self._bucket, Key=key)

        await asyncio.to_thread(_delete)

    async def presigned_get_url(self, key: str, expires_in: int = 3600) -> str:
        def _sign() -> str:
            return self._client.generate_presigned_url(
                "get_object",
                Params={"Bucket": self._bucket, "Key": key},
                ExpiresIn=expires_in,
            )

        return await asyncio.to_thread(_sign)


_storage_singleton: AudioStorage | None = None


def get_audio_storage() -> AudioStorage:
    """Return the configured storage backend.

    The first call wires the dependency: production environments use
    S3/MinIO, everything else uses the filesystem fallback. We cache the
    instance to avoid reconnecting per request.
    """

    global _storage_singleton
    if _storage_singleton is not None:
        return _storage_singleton

    settings = get_settings()
    use_s3 = settings.is_production and settings.minio_endpoint and not settings.is_test
    if use_s3:
        try:
            _storage_singleton = S3AudioStorage(settings)
            logger.info("Audio storage backend: S3 (%s)", settings.minio_endpoint)
            return _storage_singleton
        except Exception as exc:  # pragma: no cover
            logger.warning("S3 unavailable, falling back to local storage: %s", exc)

    _storage_singleton = LocalAudioStorage(settings.local_storage_dir)
    logger.info("Audio storage backend: local (%s)", settings.local_storage_dir)
    return _storage_singleton


def reset_audio_storage() -> None:
    """Test helper — drop the cached backend so it can be rebuilt."""

    global _storage_singleton
    _storage_singleton = None


def build_recording_key(
    *, assessment_id: str, recording_id: str, content_type: str
) -> str:
    """Deterministic object key per recording.

    Layout: ``audio/{assessment}/{recording}.{ext}``.
    """

    extension = _extension_for_content_type(content_type)
    safe_assessment = quote(assessment_id, safe="")
    safe_recording = quote(recording_id, safe="")
    return f"audio/{safe_assessment}/{safe_recording}.{extension}"


def _extension_for_content_type(content_type: str) -> str:
    mapping = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/wave": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/mp4": "m4a",
        "audio/x-m4a": "m4a",
        "audio/aac": "aac",
        "audio/ogg": "ogg",
        "audio/webm": "webm",
        "audio/flac": "flac",
    }
    return mapping.get(content_type.lower(), "bin")


__all__ = [
    "AudioStorage",
    "LocalAudioStorage",
    "S3AudioStorage",
    "StoredObject",
    "build_recording_key",
    "get_audio_storage",
    "reset_audio_storage",
]
