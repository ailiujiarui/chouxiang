"""Shared SQLite connection policy, journal negotiation, and safe diagnostics."""

from __future__ import annotations

import logging
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from refactor_agent.errors import is_database_locked

JournalMode = Literal["auto", "wal", "delete"]

_NETWORK_FILESYSTEMS = frozenset(
    {
        "9p",
        "afs",
        "ceph",
        "cifs",
        "fuse.sshfs",
        "glusterfs",
        "lustre",
        "nfs",
        "nfs4",
        "smbfs",
    }
)

logger = logging.getLogger(__name__)


class SQLiteConfigurationError(RuntimeError):
    """Raised before work is accepted when SQLite cannot satisfy the requested policy."""

    def __init__(self, reason: str) -> None:
        """Build a startup-safe error from a sanitized machine-readable reason."""

        self.reason = reason
        super().__init__(f"SQLite startup policy failed: {reason}")


@dataclass(frozen=True, slots=True)
class SQLitePolicy:
    """Immutable connection policy shared by every SQLite-backed Store."""

    busy_timeout_ms: int = 5_000
    journal_mode: JournalMode | str = "auto"

    def __post_init__(self) -> None:
        """Normalize and validate policy values before any database is opened."""

        normalized_mode = str(self.journal_mode).strip().casefold()
        if normalized_mode not in {"auto", "wal", "delete"}:
            raise ValueError("journal_mode must be one of: auto, wal, delete")
        if isinstance(self.busy_timeout_ms, bool) or self.busy_timeout_ms < 1:
            raise ValueError("busy_timeout_ms must be a positive integer")
        object.__setattr__(self, "journal_mode", normalized_mode)

    @classmethod
    def from_env(cls) -> "SQLitePolicy":
        """Load the one shared SQLite policy used by API, Worker, and desktop Stores."""

        return cls(
            busy_timeout_ms=int(os.getenv("REFACTOR_AGENT_SQLITE_BUSY_TIMEOUT_MS", "5000")),
            journal_mode=os.getenv("REFACTOR_AGENT_SQLITE_JOURNAL_MODE", "auto"),
        )


@dataclass(frozen=True, slots=True)
class WalSafetyGate:
    """Result of checking whether this runtime and filesystem may safely use WAL."""

    safe: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SQLiteDiagnostics:
    """Non-secret effective SQLite settings captured during Store initialization."""

    sqlite_version: str
    requested_journal_mode: str
    actual_journal_mode: str
    busy_timeout_ms: int
    foreign_keys: bool
    wal_safe: bool
    wal_gate_reason: str

    def as_public_dict(self) -> dict[str, str | int | bool]:
        """Return diagnostics that intentionally exclude paths, SQL, and payloads."""

        return asdict(self)

    def locked_summary(self, operation: str) -> str:
        """Describe an exhausted lock wait without leaking SQL, payloads, or paths."""

        safe_operation = "".join(
            character for character in operation.casefold() if character.isalnum() or character in {"_", "-"}
        )[:48]
        return (
            f"operation={safe_operation or 'sqlite_write'}; "
            f"journal_mode={self.actual_journal_mode}; "
            f"busy_timeout_ms={self.busy_timeout_ms}; wait_exhausted=true"
        )


def connect_sqlite(database_path: Path, policy: SQLitePolicy) -> sqlite3.Connection:
    """Open and verify a business connection using the common policy."""

    connection = sqlite3.connect(
        database_path,
        timeout=policy.busy_timeout_ms / 1_000,
    )
    try:
        _configure_connection(connection, policy.busy_timeout_ms)
    except Exception:
        connection.close()
        raise
    return connection


def initialize_sqlite_database(
    database_path: Path,
    policy: SQLitePolicy,
    *,
    version_info: tuple[int, ...] | None = None,
    filesystem_local: bool | None = None,
) -> SQLiteDiagnostics:
    """Negotiate journal mode once, before callers start worker/background threads."""

    version = tuple(version_info or sqlite3.sqlite_version_info)
    gate = evaluate_wal_safety(database_path, version, filesystem_local=filesystem_local)
    attempts = 2
    attempt_timeout_ms = max(1, policy.busy_timeout_ms // attempts)
    last_locked: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        try:
            return _initialize_once(
                database_path,
                policy,
                gate,
                attempt_timeout_ms=attempt_timeout_ms,
                sqlite_version=".".join(str(part) for part in version),
            )
        except sqlite3.OperationalError as exc:
            if not is_database_locked(exc) or attempt + 1 >= attempts:
                raise
            last_locked = exc
    if last_locked is not None:  # pragma: no cover - loop exhaustiveness guard
        raise last_locked
    raise RuntimeError("SQLite initialization did not produce diagnostics")


def evaluate_wal_safety(
    database_path: Path,
    version_info: tuple[int, ...] | None = None,
    *,
    filesystem_local: bool | None = None,
) -> WalSafetyGate:
    """Allow WAL only for a fixed SQLite runtime on a verified local filesystem."""

    version = tuple(version_info or sqlite3.sqlite_version_info)
    if not wal_runtime_is_safe(version):
        return WalSafetyGate(False, "sqlite_version_not_fixed")
    if filesystem_local is None:
        filesystem_local, reason = _local_filesystem_status(database_path)
    else:
        reason = "local_filesystem" if filesystem_local else "filesystem_not_local"
    if not filesystem_local:
        return WalSafetyGate(False, reason)
    return WalSafetyGate(True, "safe")


def wal_runtime_is_safe(version_info: tuple[int, ...] | None = None) -> bool:
    """Recognize upstream SQLite release lines containing the 2026 WAL-reset fix."""

    version = tuple(version_info or sqlite3.sqlite_version_info)
    major, minor, patch = (version + (0, 0, 0))[:3]
    if major != 3:
        return False
    if minor > 51:
        return True
    if minor == 51:
        return patch >= 3
    if minor == 50:
        return patch >= 7
    if minor == 44:
        return patch >= 6
    return False


def log_sqlite_diagnostics(store_name: str, diagnostics: SQLiteDiagnostics) -> None:
    """Log the effective policy using the diagnostics' deliberately public fields."""

    logger.info("SQLite store initialized: store=%s diagnostics=%s", store_name, diagnostics.as_public_dict())


def _initialize_once(
    database_path: Path,
    policy: SQLitePolicy,
    gate: WalSafetyGate,
    *,
    attempt_timeout_ms: int,
    sqlite_version: str,
) -> SQLiteDiagnostics:
    """Apply and verify one startup journal-mode decision for a database file."""

    connection = sqlite3.connect(database_path, timeout=attempt_timeout_ms / 1_000)
    try:
        _configure_connection(connection, attempt_timeout_ms)
        current_mode = _read_journal_mode(connection)
        requested_mode = str(policy.journal_mode)
        if requested_mode == "delete":
            actual_mode = _set_journal_mode(connection, "delete")
            if actual_mode != "delete":
                raise SQLiteConfigurationError("delete_mode_not_applied")
        elif requested_mode == "wal":
            if not gate.safe:
                raise SQLiteConfigurationError(f"wal_gate_failed:{gate.reason}")
            actual_mode = _set_journal_mode(connection, "wal")
            if actual_mode != "wal":
                raise SQLiteConfigurationError("wal_mode_not_applied")
        elif current_mode == "wal" and not gate.safe:
            raise SQLiteConfigurationError(f"unsafe_runtime_existing_wal:{gate.reason}")
        elif gate.safe:
            actual_mode = _set_journal_mode(connection, "wal")
            if actual_mode != "wal":
                raise SQLiteConfigurationError("wal_mode_not_applied")
        else:
            actual_mode = _set_journal_mode(connection, "delete")
            if actual_mode != "delete":
                raise SQLiteConfigurationError("auto_fallback_not_delete")

        connection.execute(f"PRAGMA busy_timeout = {policy.busy_timeout_ms}")
        busy_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        if busy_timeout != policy.busy_timeout_ms:
            raise SQLiteConfigurationError("busy_timeout_verification_failed")
        if not foreign_keys:
            raise SQLiteConfigurationError("foreign_keys_verification_failed")
        return SQLiteDiagnostics(
            sqlite_version=sqlite_version,
            requested_journal_mode=requested_mode,
            actual_journal_mode=actual_mode,
            busy_timeout_ms=busy_timeout,
            foreign_keys=foreign_keys,
            wal_safe=gate.safe,
            wal_gate_reason=gate.reason,
        )
    finally:
        connection.close()


def _configure_connection(connection: sqlite3.Connection, busy_timeout_ms: int) -> None:
    """Apply per-connection invariants and fail if SQLite does not retain them."""

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
    actual_timeout = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    foreign_keys = bool(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    if actual_timeout != busy_timeout_ms:
        raise SQLiteConfigurationError("busy_timeout_verification_failed")
    if not foreign_keys:
        raise SQLiteConfigurationError("foreign_keys_verification_failed")


def _read_journal_mode(connection: sqlite3.Connection) -> str:
    """Read the database's persistent journal mode in normalized form."""

    row = connection.execute("PRAGMA journal_mode").fetchone()
    return str(row[0]).casefold()


def _set_journal_mode(connection: sqlite3.Connection, mode: Literal["wal", "delete"]) -> str:
    """Request a journal-mode transition and return SQLite's actual mode."""

    row = connection.execute(f"PRAGMA journal_mode={mode.upper()}").fetchone()
    return str(row[0]).casefold()


def _local_filesystem_status(database_path: Path) -> tuple[bool, str]:
    """Conservatively decide whether a database path is on a local filesystem."""

    path = database_path.expanduser().absolute()
    if os.name == "nt":
        return _windows_local_filesystem_status(path)
    if os.name == "posix" and Path("/proc/self/mountinfo").is_file():
        return _linux_local_filesystem_status(path)
    return False, "filesystem_unknown"


def _windows_local_filesystem_status(path: Path) -> tuple[bool, str]:
    """Reject UNC and remote Windows drives because WAL requires local storage."""

    if str(path).startswith("\\\\"):
        return False, "filesystem_not_local"
    try:
        import ctypes

        root = path.anchor
        drive_type = int(ctypes.windll.kernel32.GetDriveTypeW(root))
    except (AttributeError, OSError, ValueError):
        return False, "filesystem_unknown"
    if drive_type == 4:
        return False, "filesystem_not_local"
    if drive_type in {2, 3, 5, 6}:
        return True, "local_filesystem"
    return False, "filesystem_unknown"


def _linux_local_filesystem_status(path: Path) -> tuple[bool, str]:
    """Resolve the longest Linux mount and reject known network filesystems."""

    selected_mount = ""
    selected_type = ""
    try:
        entries = Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines()
    except OSError:
        return False, "filesystem_unknown"
    path_text = str(path)
    for entry in entries:
        before, separator, after = entry.partition(" - ")
        if not separator:
            continue
        fields = before.split()
        filesystem_fields = after.split()
        if len(fields) < 5 or not filesystem_fields:
            continue
        mount_point = _unescape_mountinfo(fields[4])
        if _path_is_within(path_text, mount_point) and len(mount_point) > len(selected_mount):
            selected_mount = mount_point
            selected_type = filesystem_fields[0].casefold()
    if not selected_mount:
        return False, "filesystem_unknown"
    if selected_type in _NETWORK_FILESYSTEMS or selected_type.startswith("fuse.sshfs"):
        return False, "filesystem_not_local"
    return True, "local_filesystem"


def _path_is_within(path: str, mount_point: str) -> bool:
    """Return whether a path belongs to a candidate mount without prefix ambiguity."""

    if mount_point == os.sep:
        return path.startswith(os.sep)
    normalized_mount = mount_point.rstrip(os.sep)
    return path == normalized_mount or path.startswith(normalized_mount + os.sep)


def _unescape_mountinfo(value: str) -> str:
    """Decode the escape sequences used by Linux ``/proc/self/mountinfo``."""

    for encoded, decoded in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
        value = value.replace(encoded, decoded)
    return value
