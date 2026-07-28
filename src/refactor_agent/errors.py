from __future__ import annotations

import re
import sqlite3
from enum import StrEnum


class ErrorCode(StrEnum):
    LLM_AUTH_FAILED = "LLM_AUTH_FAILED"
    AUTH_FAILED = LLM_AUTH_FAILED
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    TIMEOUT = "TIMEOUT"
    CLIENT_ERROR = "CLIENT_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    INPUT_TOO_LARGE = "INPUT_TOO_LARGE"
    INJECTION_DETECTED = "INJECTION_DETECTED"
    DATABASE_LOCKED = "DATABASE_LOCKED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


_PUBLIC_MESSAGES = {
    ErrorCode.LLM_AUTH_FAILED: "The language model credentials were rejected. Check the configured credential and try again.",
    ErrorCode.RATE_LIMITED: "The language model is busy. Try again shortly.",
    ErrorCode.SERVER_ERROR: "The language model is temporarily unavailable. Try again later.",
    ErrorCode.TIMEOUT: "The language model request timed out. Try again.",
    ErrorCode.CLIENT_ERROR: "The language model request could not be completed. Try again.",
    ErrorCode.PARSE_ERROR: "The language model returned an invalid result. Try again.",
    ErrorCode.INPUT_TOO_LARGE: "The submitted content is too large. Reduce it and try again.",
    ErrorCode.INJECTION_DETECTED: "The submitted content could not be processed safely.",
    ErrorCode.DATABASE_LOCKED: "The local database is busy. Try again shortly.",
    ErrorCode.INTERNAL_ERROR: "The task could not be completed because of an internal error.",
}


def public_error_message(code: ErrorCode) -> str:
    return _PUBLIC_MESSAGES[code]


def sanitize_error_summary(summary: str) -> str:
    return re.sub(r"\s+", " ", summary).strip()[:256]


class OperationalError(RuntimeError):
    def __init__(self, code: ErrorCode, summary: str = "") -> None:
        super().__init__(summary or code.value)
        self.code = code
        self.summary = sanitize_error_summary(summary)

    @property
    def public_message(self) -> str:
        return public_error_message(self.code)


def is_database_locked(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).casefold()
    return "locked" in message or "busy" in message
