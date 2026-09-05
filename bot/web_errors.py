"""
Einheitliche Fehler für die REST-API.

Jeder Fehler landet als JSON mit maschinenlesbarem ``code`` und einer
**handlungsorientierten** deutschen Meldung — so kann eine KI den Fehler
selbst korrigieren, statt blind zu raten.

Format::

    {
      "ok": false,
      "error": {
        "code": "CHANNEL_NOT_FOUND",
        "message": "Kanal '123' existiert nicht auf diesem Server.",
        "hint": "Nutze GET /api/v1/channels für die Liste aller Kanal-IDs.",
        "status": 404,
        "request_id": "9f1c…",
        "details": { }
      }
    }
"""

from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ("ApiError",)


class ApiError(Exception):
    """Basisfehler der Relay-API."""

    default_code = "INTERNAL_ERROR"
    default_status = 500

    def __init__(
        self,
        message: str,
        *,
        code: Optional[str] = None,
        status: Optional[int] = None,
        hint: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code or self.default_code
        self.status = status or self.default_status
        self.hint = hint
        self.details = details or {}

    # ── Konstruktoren ───────────────────────────────────────────────────────
    @classmethod
    def bad_request(cls, message: str, *, hint: Optional[str] = None,
                    code: str = "BAD_REQUEST", **details: Any) -> "ApiError":
        return cls(message, code=code, status=400, hint=hint, details=details)

    @classmethod
    def unauthorized(cls, message: str, *, hint: Optional[str] = None,
                     code: str = "UNAUTHORIZED", **details: Any) -> "ApiError":
        return cls(message, code=code, status=401, hint=hint, details=details)

    @classmethod
    def forbidden(cls, message: str, *, hint: Optional[str] = None,
                  code: str = "FORBIDDEN", **details: Any) -> "ApiError":
        return cls(message, code=code, status=403, hint=hint, details=details)

    @classmethod
    def not_found(cls, message: str, *, hint: Optional[str] = None,
                  code: str = "NOT_FOUND", **details: Any) -> "ApiError":
        return cls(message, code=code, status=404, hint=hint, details=details)

    @classmethod
    def conflict(cls, message: str, *, hint: Optional[str] = None,
                 code: str = "CONFLICT", **details: Any) -> "ApiError":
        return cls(message, code=code, status=409, hint=hint, details=details)

    @classmethod
    def unavailable(cls, message: str, *, hint: Optional[str] = None,
                    code: str = "SERVICE_UNAVAILABLE") -> "ApiError":
        """503 — Dienst läuft noch nicht vollständig (z. B. Bot verbindet gerade)."""
        return cls(message, code=code, status=503, hint=hint)

    @classmethod
    def too_many(cls, message: str, *, hint: Optional[str] = None,
                 code: str = "RATE_LIMITED", retry_after: Optional[float] = None) -> "ApiError":
        return cls(message, code=code, status=429, hint=hint, details={"retry_after": retry_after})

    @classmethod
    def upstream(cls, message: str, *, hint: Optional[str] = None,
                 code: str = "DISCORD_ERROR", status: int = 502, **details: Any) -> "ApiError":
        return cls(message, code=code, status=status, hint=hint, details=details)

    # ── Serialisierung ──────────────────────────────────────────────────────
    def to_dict(self, request_id: Optional[str] = None) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": self.code, "message": self.message, "status": self.status}
        if self.hint:
            error["hint"] = self.hint
        if self.details:
            error["details"] = self.details
        if request_id:
            error["request_id"] = request_id
        return {"ok": False, "error": error}

    def __repr__(self) -> str:  # pragma: no cover
        return f"<ApiError {self.status} {self.code}: {self.message}>"
