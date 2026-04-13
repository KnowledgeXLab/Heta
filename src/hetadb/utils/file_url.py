"""S3/Ceph file URL utilities.

Provides presigned URL generation and file upload for raw knowledge-base
files stored in an S3-compatible object store (e.g. Ceph RGW).

Configuration is read from the ``s3`` section of ``config.yaml``:

    s3:
      endpoint: "https://ceph.example.com"
      ak: "<access-key>"
      sk: "<secret-key>"
      bucket: "hetadb-raw-files"

If the ``s3`` section is absent or ``endpoint`` is empty, all functions
degrade gracefully: ``s3_configured()`` returns ``False``,
``get_file_url()`` returns ``None``, and ``upload_file_to_s3()`` raises
``RuntimeError`` so callers can gate on ``s3_configured()`` first.

S3 keys follow the convention ``{dataset}/{filename}``, mirroring the
local layout ``workspace/raw_files/{dataset}/{filename}``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level singletons — initialised once on first use.
_s3_client = None          # upload/delete — uses internal endpoint
_s3_presign_client = None  # presigned URLs — uses public_endpoint so signature matches
_s3_bucket: str = ""
_s3_initialised: bool = False


def _init_s3() -> None:
    """Attempt to build the boto3 S3 clients from config. Called at most once."""
    global _s3_client, _s3_presign_client, _s3_bucket, _s3_initialised
    _s3_initialised = True

    try:
        import boto3
        from botocore.config import Config
        from hetadb.utils.load_config import get_s3_config

        cfg = get_s3_config()
        endpoint = cfg.get("endpoint", "").strip()
        if not endpoint:
            logger.debug("S3 not configured (no endpoint) — file_url will be null")
            return

        creds = dict(
            aws_access_key_id=cfg.get("ak", ""),
            aws_secret_access_key=cfg.get("sk", ""),
            config=Config(signature_version="s3v4"),
        )
        _s3_client = boto3.client("s3", endpoint_url=endpoint, **creds)
        _s3_bucket = cfg.get("bucket", "hetadb-raw-files")

        public = cfg.get("public_endpoint", "").strip().rstrip("/")
        _s3_presign_client = boto3.client(
            "s3", endpoint_url=public if public else endpoint, **creds
        )
        logger.info(
            "S3 client initialised: endpoint=%s bucket=%s presign_endpoint=%s",
            endpoint, _s3_bucket, public or endpoint,
        )

        # Auto-create bucket if it doesn't exist (e.g. fresh MinIO instance)
        try:
            _s3_client.head_bucket(Bucket=_s3_bucket)
        except Exception:
            try:
                _s3_client.create_bucket(Bucket=_s3_bucket)
                logger.info("S3 bucket created: %s", _s3_bucket)
            except Exception:
                logger.warning(
                    "S3 bucket '%s' does not exist and auto-creation failed — "
                    "uploads will fail until the bucket is created manually",
                    _s3_bucket, exc_info=True,
                )

    except ImportError:
        logger.warning("boto3 not installed — S3 support unavailable")
    except Exception:
        logger.warning("S3 client init failed — file_url will be null", exc_info=True)


def _get_client():
    """Return ``(client, bucket)`` for upload/delete operations."""
    if not _s3_initialised:
        _init_s3()
    return _s3_client, _s3_bucket


def _get_presign_client():
    """Return ``(client, bucket)`` for presigned URL generation."""
    if not _s3_initialised:
        _init_s3()
    return _s3_presign_client, _s3_bucket


def s3_configured() -> bool:
    """Return True if S3 is configured and the client was initialised successfully."""
    client, _ = _get_client()
    return client is not None


def get_file_url(dataset: str, source_file: str, expires_in: int = 900) -> str | None:
    """Return a presigned S3 GET URL for ``{dataset}/{source_file}``.

    The URL is valid for *expires_in* seconds (default 15 minutes).

    If ``s3.public_endpoint`` is configured, the internal endpoint in the
    presigned URL is replaced with it.  This is useful when the backend
    connects to MinIO via a Docker-internal hostname (e.g. ``http://minio:9000``)
    but clients need a publicly reachable address (e.g. ``http://localhost:9000``
    or ``https://files.yourdomain.com``).

    Returns ``None`` if S3 is not configured or presigned URL generation fails.
    """
    if not source_file:
        return None
    client, bucket = _get_client()
    if client is None:
        return None
    try:
        presign_client, _ = _get_presign_client()
        url = presign_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": f"{dataset}/{source_file}"},
            ExpiresIn=expires_in,
        )
        return url
    except Exception:
        logger.warning(
            "Failed to generate presigned URL for %s/%s", dataset, source_file,
            exc_info=True,
        )
        return None


def upload_file_to_s3(dataset: str, filename: str, local_path: str) -> None:
    """Upload *local_path* to S3 at key ``{dataset}/{filename}``.

    Raises ``RuntimeError`` if S3 is not configured.
    Raises any boto3 exception on upload failure so the caller can roll back.
    """
    client, bucket = _get_client()
    if client is None:
        raise RuntimeError(
            "S3 upload requested but S3 is not configured. "
            "Check the 's3' section in config.yaml."
        )
    client.upload_file(
        local_path,
        bucket,
        f"{dataset}/{filename}",
        ExtraArgs={"ChecksumAlgorithm": "SHA256"},
    )
    logger.info("Uploaded %s/%s to S3 bucket %s", dataset, filename, bucket)


def delete_file_from_s3(dataset: str, filename: str) -> bool:
    """Delete the object at ``{dataset}/{filename}`` from S3.

    Returns ``True`` if the request was sent successfully, ``False`` if S3 is
    not configured (no-op).  S3 ``DeleteObject`` is idempotent — it succeeds
    even when the key does not exist, so no special handling is needed for
    "already gone" cases.

    Raises ``botocore.exceptions.ClientError`` on unexpected S3 errors so the
    caller can decide whether to surface them.
    """
    if not filename:
        return False
    client, bucket = _get_client()
    if client is None:
        return False
    key = f"{dataset}/{filename}"
    client.delete_object(Bucket=bucket, Key=key)
    logger.info("Deleted s3://%s/%s", bucket, key)
    return True


def delete_dataset_from_s3(dataset: str) -> int:
    """Delete every object under the ``{dataset}/`` prefix from S3.

    Uses paginated ``ListObjectsV2`` + ``DeleteObjects`` (≤ 1 000 keys per
    request) as prescribed by the S3 API reference.  Returns the number of
    objects successfully deleted.

    Returns 0 without error when S3 is not configured or the prefix is empty.
    Raises ``botocore.exceptions.ClientError`` on unexpected S3 errors.
    """
    client, bucket = _get_client()
    if client is None:
        return 0

    prefix = f"{dataset}/"
    deleted = 0
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        objects = page.get("Contents", [])
        if not objects:
            continue
        # Use individual delete_object calls to avoid the Content-MD5 header
        # requirement imposed by some S3-compatible endpoints (e.g. Ceph).
        for obj in objects:
            try:
                client.delete_object(Bucket=bucket, Key=obj["Key"])
                deleted += 1
            except Exception as e:
                logger.warning("S3 delete_object failed for %s: %s", obj["Key"], e)

    logger.info("Deleted %d object(s) from s3://%s/%s", deleted, bucket, prefix)
    return deleted
