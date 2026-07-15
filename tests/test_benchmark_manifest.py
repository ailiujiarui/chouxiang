from pathlib import Path
import shutil

import pytest

from refactor_agent.benchmark_manifest import load_manifest


MANIFEST = Path("benchmarks/manifest.toml")


def test_public_benchmark_manifest_pins_eight_approved_cases():
    manifest = load_manifest(MANIFEST)

    assert [case.name for case in manifest.cases] == [
        "more-take-off-by-one",
        "more-chunked-strict",
        "more-first-default",
        "boltons-clamp-bounds",
        "boltons-camel-boundary",
        "boltons-chunk-overlap",
        "sorted-list-contains",
        "sorted-set-contains",
    ]
    assert {case.repository for case in manifest.cases} == {
        "more-itertools/more-itertools",
        "mahmoud/boltons",
        "grantjenks/python-sortedcontainers",
    }
    assert all(len(case.commit) == 40 for case in manifest.cases)
    assert all(case.expected_status in {"SUCCESS", "FAILED"} for case in manifest.cases)
    assert all(case.seed_patch.is_file() for case in manifest.cases)
    assert all(case.gold_snapshot.is_file() for case in manifest.cases)


def test_manifest_hash_is_stable_for_identical_content(tmp_path: Path):
    first = load_manifest(MANIFEST)
    copied = tmp_path / "manifest.toml"
    copied.write_bytes(MANIFEST.read_bytes())
    shutil.copytree(MANIFEST.parent / "cases", tmp_path / "cases")

    second = load_manifest(copied)

    assert first.manifest_hash == second.manifest_hash


def test_manifest_hash_changes_when_gold_fixture_changes(tmp_path: Path):
    copied = tmp_path / "manifest.toml"
    copied.write_bytes(MANIFEST.read_bytes())
    shutil.copytree(MANIFEST.parent / "cases", tmp_path / "cases")
    before = load_manifest(copied).manifest_hash

    gold = tmp_path / "cases" / "more-take-off-by-one" / "gold.py"
    gold.write_text(gold.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    assert load_manifest(copied).manifest_hash != before


def test_manifest_rejects_noncanonical_repository_and_short_sha(tmp_path: Path):
    path = tmp_path / "manifest.toml"
    path.write_text(
        """
version = 1
[[cases]]
name = "bad"
category = "correctness"
repository = "https://example.com/repo"
commit = "abc"
target = "value.py"
tests = "tests"
issue = "fix"
expected_status = "SUCCESS"
seed_patch = "seed.patch"
gold_snapshot = "gold.py"
allowed_import_roots = []
docker_test_command = ["python", "-m", "pytest", "tests"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError):
        load_manifest(path)


def test_manifest_rejects_fixture_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside-gold.py"
    outside.write_text("def value():\n    return 1\n", encoding="utf-8")
    seed = tmp_path / "seed.patch"
    seed.write_text("patch", encoding="utf-8")
    link = tmp_path / "gold.py"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks unavailable")
    path = tmp_path / "manifest.toml"
    path.write_text(
        """
version = 1
[[cases]]
name = "symlink-escape"
category = "security"
repository = "octo/demo"
commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
target = "value.py"
tests = "tests"
issue = "fix"
expected_status = "SUCCESS"
seed_patch = "seed.patch"
gold_snapshot = "gold.py"
allowed_import_roots = []
docker_test_command = ["python", "-m", "pytest", "tests"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes"):
        load_manifest(path)
