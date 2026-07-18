from __future__ import annotations

import hashlib
import json
import re
import tomllib
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class BenchmarkCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]+$")
    category: str
    repository: str
    commit: str
    target: str
    tests: str
    issue: str
    expected_status: Literal["SUCCESS", "FAILED"]
    seed_patch: Path
    gold_snapshot: Path
    allowed_import_roots: tuple[str, ...] = ()
    docker_test_command: tuple[str, ...]

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            raise ValueError("repository must be an owner/repo GitHub identity")
        return value

    @field_validator("commit")
    @classmethod
    def validate_commit(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("commit must be a full lowercase SHA-1")
        return value

    @field_validator("target", "tests")
    @classmethod
    def validate_repo_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not value.strip():
            raise ValueError("benchmark repository paths must be relative")
        return str(path)

    @field_validator("docker_test_command")
    @classmethod
    def validate_test_command(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) < 4 or value[:3] != ("python", "-m", "pytest"):
            raise ValueError("docker_test_command must invoke python -m pytest")
        return value


class BenchmarkManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    version: Literal[1]
    cases: tuple[BenchmarkCase, ...]
    manifest_hash: str


def load_manifest(path: Path) -> BenchmarkManifest:
    manifest_path = path.resolve()
    raw_bytes = manifest_path.read_bytes()
    raw = tomllib.loads(raw_bytes.decode("utf-8"))
    cases_raw = raw.get("cases")
    if not isinstance(cases_raw, list) or not cases_raw:
        raise ValueError("manifest must contain at least one [[cases]] entry")
    case_names: set[str] = set()
    cases: list[BenchmarkCase] = []
    for raw_case in cases_raw:
        if not isinstance(raw_case, dict):
            raise ValueError("manifest case must be a table")
        data = dict(raw_case)
        for key in ("seed_patch", "gold_snapshot"):
            relative = _relative_manifest_path(str(data[key]))
            resolved = (manifest_path.parent / Path(*relative.parts)).resolve()
            if not resolved.is_relative_to(manifest_path.parent):
                raise ValueError(f"benchmark fixture escapes manifest directory: {data[key]}")
            data[key] = resolved
        case = BenchmarkCase.model_validate(data)
        if case.name in case_names:
            raise ValueError(f"duplicate benchmark case: {case.name}")
        case_names.add(case.name)
        if not case.seed_patch.is_file() or not case.gold_snapshot.is_file():
            raise ValueError(f"benchmark fixture missing for case {case.name}")
        cases.append(case)
    fixture_hashes = {
        case.name: {
            "seed_patch": hashlib.sha256(case.seed_patch.read_bytes()).hexdigest(),
            "gold_snapshot": hashlib.sha256(case.gold_snapshot.read_bytes()).hexdigest(),
        }
        for case in cases
    }
    canonical = json.dumps(
        {"manifest": raw, "fixtures": fixture_hashes},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return BenchmarkManifest(
        version=raw.get("version"),
        cases=tuple(cases),
        manifest_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    )


def _relative_manifest_path(value: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or not value.strip():
        raise ValueError("manifest fixture paths must be relative")
    return path
