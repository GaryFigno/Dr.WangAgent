"""Persist and rehydrate user-pasted images for a session.

Images are stored as files under ``<session>/attachments/`` so the append-only
``messages.jsonl`` stays small. Message ``meta["attachments"]`` holds ordered
refs; the wire layer expands them into OpenAI multimodal ``image_url`` parts.
"""

from __future__ import annotations

import base64
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config.schema import ModelDef
from ..constants import (
    ATTACHMENT_ALLOWED_MIMES,
    ATTACHMENT_MAX_BYTES,
    ATTACHMENT_MAX_COUNT,
    VISION_MODEL_IDS,
    VISION_MODEL_MARKERS,
)
from ..providers.base import Message

VISION_MODE_ON = frozenset({"on", "force_on", "true", "1", "yes"})
VISION_MODE_OFF = frozenset({"off", "force_off", "false", "0", "no"})

ATTACHMENTS_DIR = "attachments"

_MIME_TO_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class ImageAttachment:
    """One ordered image ready to persist or already on disk."""

    mime: str
    data: bytes
    name: str = ""

    @property
    def normalised_mime(self) -> str:
        mime = (self.mime or "").strip().lower()
        if mime == "image/jpg":
            return "image/jpeg"
        return mime


@dataclass(frozen=True)
class AttachmentRef:
    """Durable pointer written into message meta."""

    file: str
    mime: str
    name: str
    index: int

    def to_meta(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "mime": self.mime,
            "name": self.name,
            "index": self.index,
        }


class AttachmentError(ValueError):
    """User-facing validation failure for a pasted image."""


def decode_data_url_or_b64(raw: str, mime_hint: str = "") -> tuple[str, bytes]:
    """Decode a ``data:`` URL or bare base64 payload into ``(mime, bytes)``."""
    text = (raw or "").strip()
    if text.startswith("data:"):
        header, _, payload = text.partition(",")
        if not payload or ";base64" not in header:
            raise AttachmentError("image data URL must be base64-encoded")
        mime = header[5:].split(";", 1)[0].strip().lower() or mime_hint
        try:
            return mime, base64.b64decode(payload, validate=False)
        except Exception as error:  # noqa: BLE001
            raise AttachmentError(f"invalid image data: {error}") from error
    try:
        return mime_hint, base64.b64decode(text, validate=False)
    except Exception as error:  # noqa: BLE001
        raise AttachmentError(f"invalid image data: {error}") from error


def parse_inbound_images(raw: Any) -> list[ImageAttachment]:
    """Validate the ``images`` list from a GUI ``prompt`` command."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise AttachmentError("images must be a list")
    if len(raw) > ATTACHMENT_MAX_COUNT:
        raise AttachmentError(f"at most {ATTACHMENT_MAX_COUNT} images per message")

    images: list[ImageAttachment] = []
    for item in raw:
        if not isinstance(item, dict):
            raise AttachmentError("each image must be an object")
        mime = str(item.get("mime") or "").strip().lower()
        name = str(item.get("name") or "").strip()
        data_field = item.get("data")
        if not isinstance(data_field, str) or not data_field.strip():
            raise AttachmentError("each image needs base64 data")
        decoded_mime, data = decode_data_url_or_b64(data_field, mime)
        mime = (decoded_mime or mime).lower()
        if mime == "image/jpg":
            mime = "image/jpeg"
        if mime not in ATTACHMENT_ALLOWED_MIMES:
            raise AttachmentError(f"unsupported image type: {mime or '(missing)'}")
        if not data:
            raise AttachmentError("empty image data")
        if len(data) > ATTACHMENT_MAX_BYTES:
            raise AttachmentError(
                f"image exceeds {ATTACHMENT_MAX_BYTES // (1024 * 1024)} MB limit"
            )
        images.append(ImageAttachment(mime=mime, data=data, name=name or "paste"))
    return images


def save_attachments(
    session_dir: Path, images: list[ImageAttachment]
) -> list[AttachmentRef]:
    """Write ordered images under ``session_dir/attachments`` and return refs."""
    if not images:
        return []
    folder = session_dir / ATTACHMENTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    refs: list[AttachmentRef] = []
    for index, image in enumerate(images, start=1):
        mime = image.normalised_mime
        ext = _MIME_TO_EXT.get(mime, ".bin")
        stem = _SAFE_NAME.sub("-", (image.name or "paste").rsplit(".", 1)[0])[:40] or "paste"
        filename = f"{index:02d}-{stem}-{uuid.uuid4().hex[:8]}{ext}"
        path = folder / filename
        path.write_bytes(image.data)
        refs.append(
            AttachmentRef(
                file=f"{ATTACHMENTS_DIR}/{filename}",
                mime=mime,
                name=image.name or filename,
                index=index,
            )
        )
    return refs


def attachment_labels(refs: list[AttachmentRef]) -> str:
    """Human-readable placeholders when the model cannot see images."""
    if not refs:
        return ""
    lines = [f"[图{ref.index}: {ref.name}]" for ref in refs]
    return "\n".join(lines)


def load_attachment_bytes(session_dir: Path, ref: dict[str, Any]) -> bytes | None:
    """Read one attachment file; return None if missing or unsafe."""
    relative = str(ref.get("file") or "")
    if not relative or ".." in relative.replace("\\", "/").split("/"):
        return None
    path = (session_dir / relative).resolve()
    try:
        path.relative_to(session_dir.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path.read_bytes()


def multimodal_parts(
    text: str,
    session_dir: Path,
    refs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build OpenAI-style multimodal content parts for one user turn."""
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for ref in refs:
        data = load_attachment_bytes(session_dir, ref)
        if data is None:
            continue
        mime = str(ref.get("mime") or "image/png")
        encoded = base64.b64encode(data).decode("ascii")
        parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{encoded}"},
            }
        )
    return parts


def refs_from_meta(meta: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return ordered attachment dicts from message meta."""
    raw = (meta or {}).get("attachments") or []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def infer_vision_capability(model: ModelDef | None) -> bool:
    """Name / tag heuristics only (no user override). Used when stamping auto."""
    if model is None:
        return False
    if "vision" in {tag.lower() for tag in model.tags}:
        return True
    ids = {
        str(model.model or "").strip().lower(),
        str(model.id or "").strip().lower(),
    }
    if ids & VISION_MODEL_IDS:
        return True
    lowered = f"{model.model} {model.id}".lower()
    return any(marker in lowered for marker in VISION_MODEL_MARKERS)


def model_supports_vision(model: ModelDef | None) -> bool:
    """Whether pasted images should be sent as multimodal content.

    ``vision_mode``:
      * ``on`` / ``off`` — user lock (wins)
      * ``auto`` — cached ``supports_vision`` from probe, else name heuristics

    DeepSeek V4 public API is text-only; heuristics never invent vision for it.
    """
    if model is None:
        return False
    mode = str(getattr(model, "vision_mode", "auto") or "auto").strip().lower()
    if mode in VISION_MODE_ON:
        return True
    if mode in VISION_MODE_OFF:
        return False
    if model.supports_vision:
        return True
    return infer_vision_capability(model)


def expand_message_for_wire(
    message: Message,
    session_dir: Path | None,
    *,
    include_images: bool,
) -> Message:
    """Return a wire-ready copy with multimodal parts when vision is on."""
    refs = refs_from_meta(message.meta)
    if (
        message.role != "user"
        or not refs
        or not include_images
        or session_dir is None
        or isinstance(message.content, list)
    ):
        return message
    text = message.content if isinstance(message.content, str) else ""
    parts = multimodal_parts(text, session_dir, refs)
    if len(parts) == 1:
        return message
    return Message(
        role=message.role,
        content=parts,
        tool_calls=message.tool_calls,
        tool_call_id=message.tool_call_id,
        name=message.name,
        reasoning=message.reasoning,
        meta=message.meta,
    )
