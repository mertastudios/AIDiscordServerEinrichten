"""
Bild-Eingaben auflösen: URL, Data-URI oder ``null`` (entfernen).

Discord erwartet Bilder als Bytes (PNG/JPEG/GIF/WebP). Die API nimmt deshalb
drei Formen an — damit eine KI nicht selbst base64 bauen muss:

``"icon": "https://…/logo.png"``   → wird serverseitig geladen
``"icon": "data:image/png;base64,…"``→ wird direkt dekodiert
``"icon": null`` oder ``"remove"`` → Bild wird entfernt
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any, Optional

import aiohttp

from ..util import ApiError

__all__ = ("resolve_image", "is_image_field")

_DATA_URI = re.compile(r"^data:(?P<mime>image/[a-zA-Z0-9.+-]+);base64,(?P<data>.+)$", re.DOTALL)
_ALLOWED_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_REMOVE_WORDS = {"remove", "remove it", "löschen", "loeschen", "entfernen", "none", "null", "clear", "delete"}


def is_image_field(data: Any, key: str) -> bool:
    return isinstance(data, dict) and key in data


async def resolve_image(
    value: Any,
    *,
    session: aiohttp.ClientSession,
    field: str = "image",
    max_bytes: int = 10 * 1024 * 1024,
    allow_removal: bool = True,
) -> Optional[bytes]:
    """
    Liefert Bytes für Discord oder ``None`` (= Bild entfernen).

    Wirft :class:`ApiError` bei allem anderen — inklusive Größen- und
    Format-Checks, damit Discord nicht mit einem kryptischen 50026 antwortet.
    """
    if value is None:
        if allow_removal:
            return None
        raise ApiError.bad_request(f"{field}: null ist hier nicht erlaubt.")

    if isinstance(value, (bytes, bytearray)):
        data = bytes(value)
        _check_size(data, field, max_bytes)
        return data

    if not isinstance(value, str):
        raise ApiError.bad_request(
            f"{field}: erwartet eine Bild-URL, einen 'data:image/…;base64,…'-String oder null "
            f"(erhalten: {type(value).__name__})."
        )

    text = value.strip()
    if not text:
        raise ApiError.bad_request(f"{field}: leerer String. Nutze null zum Entfernen.")

    if text.lower() in _REMOVE_WORDS:
        if allow_removal:
            return None
        raise ApiError.bad_request(f"{field}: Entfernen ist hier nicht erlaubt.")

    match = _DATA_URI.match(text)
    if match:
        mime = match.group("mime").lower()
        if mime not in _ALLOWED_MIME:
            raise ApiError.bad_request(
                f"{field}: MIME-Typ '{mime}' wird von Discord nicht unterstützt.",
                hint=f"Erlaubt: {', '.join(sorted(_ALLOWED_MIME))}",
                code="IMAGE_FORMAT_UNSUPPORTED",
            )
        try:
            data = base64.b64decode(match.group("data"), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ApiError.bad_request(f"{field}: Base64-Daten sind ungültig ({exc}).") from exc
        _check_size(data, field, max_bytes)
        return data

    if re.match(r"^https?://", text, re.IGNORECASE):
        try:
            async with session.get(text, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    raise ApiError.bad_request(
                        f"{field}: Bild konnte nicht geladen werden — {text} antwortet mit HTTP {response.status}.",
                        hint="Die URL muss öffentlich erreichbar sein und direkt auf die Bilddatei zeigen.",
                        code="IMAGE_FETCH_FAILED",
                    )
                content_type = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if content_type and not content_type.startswith("image/"):
                    raise ApiError.bad_request(
                        f"{field}: '{text}' liefert '{content_type}' statt eines Bildes.",
                        hint="Nutze einen direkten Link auf eine PNG/JPEG/GIF/WebP-Datei.",
                        code="IMAGE_NOT_AN_IMAGE",
                    )
                data = await response.read()
        except ApiError:
            raise
        except aiohttp.ClientError as exc:
            raise ApiError.bad_request(
                f"{field}: Bild-Download fehlgeschlagen ({exc.__class__.__name__}: {exc}).",
                hint="Ist die URL öffentlich erreichbar? Keine Auth nötig?",
                code="IMAGE_FETCH_FAILED",
            ) from exc
        if not data:
            raise ApiError.bad_request(f"{field}: Die URL liefert eine leere Datei.")
        _check_size(data, field, max_bytes)
        return data

    raise ApiError.bad_request(
        f"{field}: '{text[:80]}' ist weder eine http(s)-URL, ein Data-URI noch 'remove'.",
        hint='Beispiele: "https://example.com/logo.png", '
        '"data:image/png;base64,iVBORw0KG…", null',
        code="IMAGE_INPUT_INVALID",
    )


def _check_size(data: bytes, field: str, max_bytes: int) -> None:
    if len(data) > max_bytes:
        raise ApiError.bad_request(
            f"{field}: Bild ist zu groß ({len(data) / 1024 / 1024:.2f} MiB, "
            f"maximal {max_bytes / 1024 / 1024:.2f} MiB).",
            code="IMAGE_TOO_LARGE",
        )
    if len(data) < 16:
        raise ApiError.bad_request(f"{field}: Bilddaten sind verdächtig klein ({len(data)} Bytes).")
