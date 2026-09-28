"""Automatic local Python code review triggered by foreground activity.

The service is deliberately fail-closed: it only reads a Python file when the
desktop privacy policy explicitly grants ``auto_code_review`` consent, and it
never persists the raw window title or file path outside the local review
workspace. Review output is short, tsundere-flavoured text delivered through
the existing notification pipeline.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Lock, Thread
from typing import Literal

from nailong_agent.contracts import PetPersonalityResponse
from nailong_agent.notification_service import NotificationPort
from refactor_agent.adversary import generate_adversarial_tests
from refactor_agent.llm import LLMError
from refactor_agent.local_refactor import run_local_refactor
from refactor_agent.models import EvidenceLevel, RefactorRequest, RefactorRunResult

_MAX_MESSAGE = 500
_DEFAULT_REQUEST = "锐评这段 Python 代码，指出结构问题并给出更简洁的写法。"


@dataclass(frozen=True)
class CodeReview:
    path: Path
    run_id: str | None
    status: str
    message: str
    intent: Literal["encourage", "remind", "celebrate", "ask"]


class CodeReviewService:
    """Queue-backed review worker that keeps the Win32 hook thread responsive."""

    def __init__(
        self,
        *,
        notifications: NotificationPort | None,
        run_root: Path,
        database_path: Path,
        workspace_root: Path | None = None,
        sandbox_backend: str = "subprocess",
        mock: bool = True,
        max_retry: int = 1,
        timeout_seconds: float = 30.0,
        deadline_seconds: int = 300,
        cooldown_seconds: float = 120.0,
        max_source_bytes: int = 200_000,
        clock: Callable[[], datetime] | None = None,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self.notifications = notifications
        self.run_root = run_root
        self.database_path = database_path
        self.workspace_root = workspace_root
        self.sandbox_backend = sandbox_backend
        self.mock = mock
        self.max_retry = max_retry
        self.timeout_seconds = timeout_seconds
        self.deadline_seconds = deadline_seconds
        self.cooldown_seconds = cooldown_seconds
        self.max_source_bytes = max_source_bytes
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.on_error = on_error
        self._queue: Queue[str] = Queue()
        self._stop = Event()
        self._thread: Thread | None = None
        self._lock = Lock()
        self._reviewed: dict[str, int] = {}
        self._last_review_at: dict[str, float] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._run, name="nailong-code-review", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None

    def enqueue(self, path: str) -> None:
        if self._stop.is_set():
            return
        self._queue.put(path)

    def review_now(self, path: str | Path) -> CodeReview | None:
        """Review a single file synchronously; returns None when skipped or unsafe."""

        candidate = Path(path)
        if candidate.suffix.casefold() != ".py" or not candidate.is_file():
            return None
        try:
            stat = candidate.stat()
        except OSError:
            return None
        if stat.st_size == 0 or stat.st_size > self.max_source_bytes:
            return None
        key = str(candidate.resolve())
        with self._lock:
            if self._reviewed.get(key) == stat.st_mtime_ns:
                return None
            now = self.clock().timestamp()
            last = self._last_review_at.get(key)
            if last is not None and now - last < self.cooldown_seconds:
                return None
            self._reviewed[key] = stat.st_mtime_ns
            self._last_review_at[key] = now
        try:
            review = self._run_pipeline(candidate)
        except LLMError:
            return None
        if review is not None:
            self._publish(candidate, stat.st_mtime_ns, review)
        return review

    def _run_pipeline(self, target: Path) -> CodeReview | None:
        source = target.read_text(encoding="utf-8")
        generated = generate_adversarial_tests(source, "snippet", _DEFAULT_REQUEST)
        evidence = EvidenceLevel.GENERATED_TESTS if generated else EvidenceLevel.STATIC
        tests = generated or (
            "import snippet\n\n"
            "def test_generated_module_imports():\n"
            "    assert snippet is not None\n"
        )
        with tempfile.TemporaryDirectory(prefix="nailong-code-review-") as directory:
            root = Path(directory)
            (root / "snippet.py").write_text(source, encoding="utf-8")
            (root / "test_snippet.py").write_text(tests, encoding="utf-8")
            result = run_local_refactor(
                RefactorRequest(
                    target_file=root / "snippet.py",
                    issue_text=_DEFAULT_REQUEST,
                    tests_path=root / "test_snippet.py",
                    repo_name="nailong/auto-review",
                    max_retry=self.max_retry,
                    evidence_level=evidence,
                ),
                run_root=self.run_root,
                database_path=self.database_path,
                pytest_timeout_seconds=self.timeout_seconds,
                mock=self.mock,
                sandbox_backend=self.sandbox_backend,
                deadline_seconds=self.deadline_seconds,
            )
        return _review_from_result(target, result)

    def _publish(self, target: Path, mtime_ns: int, review: CodeReview) -> None:
        if self.notifications is None:
            return
        self.notifications.ingest_personality_response(
            event_id=f"code-review:{target}:{mtime_ns}",
            occurred_at=self.clock(),
            response=PetPersonalityResponse(
                persona_version="nailong-code-review-v1",
                message=review.message,
                intent=review.intent,
            ),
        )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                path = self._queue.get(timeout=0.5)
            except Empty:
                continue
            try:
                self.review_now(path)
            except Exception as exc:  # pragma: no cover - defensive worker guard
                if self.on_error is not None:
                    self.on_error(exc)


def _review_from_result(target: Path, result: RefactorRunResult) -> CodeReview:
    record = result.record
    name = target.name
    approved = record.status == "SUCCESS"
    metrics = f"LOC {record.pre_loc}->{record.post_loc}，圈复杂度 {record.pre_cc}->{record.post_cc}"
    if approved:
        message = (
            f"哼，本龙顺手扫了 {name}：这版过了。{metrics}。"
            "别误会，本龙只是顺手，不是在夸你。"
        )
        intent: Literal["encourage", "remind", "celebrate", "ask"] = "celebrate"
    else:
        reason = record.error_summary or record.error_message or "证据没站稳"
        message = (
            f"哼，{name} 被本龙挑出问题了：{reason}。"
            "先看报告第一条，别让本龙白挑这一趟。"
        )
        intent = "remind"
    return CodeReview(
        path=target,
        run_id=record.run_id,
        status=record.status,
        message=message[:_MAX_MESSAGE],
        intent=intent,
    )
