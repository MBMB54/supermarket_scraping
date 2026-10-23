import hashlib
import json

IMAGE_PREFIX = "images"


def content_key(data: str | bytes) -> str:
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def stable_hash(fields: dict) -> str:
    """Order-independent hash of JSON-serialisable fields; 16 hex chars."""
    return content_key(json.dumps(fields, sort_keys=True, separators=(",", ":")))[:16]


def image_key(sha256: str) -> str:
    return f"{IMAGE_PREFIX}/{sha256[:2]}/{sha256}"


def input_key(modality: str, text: str | None, image_sha256: str | None) -> str:
    """Key of exactly what the model sees for one row; a change in either input changes it."""
    if modality == "text":
        return content_key(text)
    if modality == "image":
        return image_sha256
    return content_key(f"{image_sha256}\x00{text}")
