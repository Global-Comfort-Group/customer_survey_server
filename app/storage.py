"""S3-compatible object storage (Tigris, attached to Railway as CSS_OSS).

Credentials arrive as the standard AWS_* variables that the storage
integration injects into the service. When they are absent the module reports
itself disabled and callers return 503 rather than crashing, which keeps the
SQLite test suite and local development working without credentials — the same
approach `app.email` takes with RESEND_API_KEY.

Downloads are always presigned GETs: the bucket does not implement bucket
policies, so there is no public-read path to fall back on.
"""

from __future__ import annotations

import os
import uuid
from typing import Any, Optional

# Uploads go straight from the browser to the bucket, so these limits are
# enforced by the presigned POST policy rather than by application code.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
UPLOAD_URL_TTL_SECONDS = 300
DOWNLOAD_URL_TTL_SECONDS = 300

ALLOWED_CONTENT_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "application/pdf": ".pdf",
}

_ENDPOINT = os.getenv("AWS_ENDPOINT_URL", "")
_BUCKET = os.getenv("AWS_S3_BUCKET_NAME", "")
_REGION = os.getenv("AWS_DEFAULT_REGION", "auto")

_client = None


def is_enabled() -> bool:
    """True when the bucket credentials are present and boto3 is importable."""
    return bool(
        _ENDPOINT
        and _BUCKET
        and os.getenv("AWS_ACCESS_KEY_ID")
        and os.getenv("AWS_SECRET_ACCESS_KEY")
    )


def _get_client():
    global _client
    if _client is None:
        import boto3
        from botocore.config import Config

        _client = boto3.client(
            "s3",
            endpoint_url=_ENDPOINT,
            region_name=_REGION,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )
    return _client


def build_key(owner_type: str, owner_id: str, attachment_id: str, content_type: str) -> str:
    """Object key. Grouped by owner so a survey's files stay browsable in the
    bucket, and suffixed so downloads get a sensible extension."""
    ext = ALLOWED_CONTENT_TYPES.get(content_type, "")
    return f"{owner_type}/{owner_id or 'misc'}/{attachment_id}{ext}"


def new_attachment_id() -> str:
    return str(uuid.uuid4())


def presign_upload(key: str, content_type: str, max_bytes: int = MAX_UPLOAD_BYTES) -> dict[str, Any]:
    """Presigned POST. The content-length-range condition is enforced by the
    bucket, so an oversized body is rejected before it reaches us."""
    return _get_client().generate_presigned_post(
        Bucket=_BUCKET,
        Key=key,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, max_bytes],
        ],
        ExpiresIn=UPLOAD_URL_TTL_SECONDS,
    )


def presign_download(key: str, filename: str) -> str:
    params: dict[str, Any] = {"Bucket": _BUCKET, "Key": key}
    if filename:
        safe = filename.replace('"', "")
        params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
    return _get_client().generate_presigned_url(
        "get_object", Params=params, ExpiresIn=DOWNLOAD_URL_TTL_SECONDS
    )


def head(key: str) -> Optional[dict[str, Any]]:
    """Object metadata, or None when it does not exist. Used to confirm that
    an upload actually landed instead of trusting the client's word."""
    try:
        return _get_client().head_object(Bucket=_BUCKET, Key=key)
    except Exception:
        return None


def delete(key: str) -> None:
    try:
        _get_client().delete_object(Bucket=_BUCKET, Key=key)
    except Exception:
        # Deleting storage is best-effort: a missing object is the desired
        # end state anyway, and a bucket hiccup must not block the DB write.
        pass


def put_bucket_cors(origins: list[str]) -> None:
    """Allow browsers on `origins` to POST directly to the bucket."""
    _get_client().put_bucket_cors(
        Bucket=_BUCKET,
        CORSConfiguration={
            "CORSRules": [
                {
                    "AllowedOrigins": origins,
                    "AllowedMethods": ["GET", "PUT", "POST", "HEAD"],
                    "AllowedHeaders": ["*"],
                    "ExposeHeaders": ["ETag", "Location"],
                    "MaxAgeSeconds": 3600,
                }
            ]
        },
    )
