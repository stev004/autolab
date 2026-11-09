import json
import shutil
import subprocess
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from uuid import UUID

import pytest

from autonomous_lab.provenance.hashing import canonical_json, canonical_sha256, sha256_file
from autonomous_lab.provenance.runtime import capture_runtime_metadata


def test_canonical_hash_is_independent_of_mapping_and_set_order() -> None:
    left = {"b": {"z", "a"}, "a": Decimal("1.000")}
    right = {"a": Decimal("1"), "b": {"a", "z"}}

    assert canonical_json(left) == canonical_json(right)
    assert canonical_sha256(left) == canonical_sha256(right)


def test_canonical_json_normalizes_identifiers_and_utc_time() -> None:
    value = {
        "id": UUID("00000000-0000-0000-0000-000000000001"),
        "at": datetime(2026, 8, 14, 12, 0, tzinfo=UTC),
    }

    assert canonical_json(value) == (
        '{"at":"2026-08-14T12:00:00.000000Z","id":"00000000-0000-0000-0000-000000000001"}'
    )


def test_canonical_json_rejects_naive_timestamp() -> None:
    with pytest.raises(ValueError, match="naive"):
        canonical_json({"at": datetime(2026, 8, 14)})


def test_canonical_decimal_preserves_all_digits_in_every_decimal_context() -> None:
    left = Decimal("0.12345678901234567890123456781")
    right = Decimal("0.12345678901234567890123456782")
    hashes: set[str] = set()
    for precision in (6, 28, 80):
        with localcontext() as context:
            context.prec = precision
            assert canonical_sha256(left) != canonical_sha256(right)
            hashes.add(canonical_sha256(left))
            assert canonical_json(left) == f'"{left}"'
    assert len(hashes) == 1


@pytest.mark.parametrize("exponent", [1_000_000, -1_000_000])
def test_extreme_decimal_hashing_uses_bounded_exact_notation(exponent: int) -> None:
    value = Decimal(f"1e{exponent}")
    encoded = canonical_json(value)
    assert len(encoded) < 30
    assert Decimal(encoded.strip('"')) == value
    assert canonical_sha256(value) == canonical_sha256(Decimal(f"10e{exponent - 1}"))


@pytest.mark.parametrize(
    ("value", "canonical"),
    [("1.000", "1"), ("1200.00", "1200"), ("0.00010", "0.0001"), ("-1.230", "-1.23")],
)
def test_ordinary_decimal_hash_spelling_is_unchanged(value: str, canonical: str) -> None:
    assert canonical_json(Decimal(value)) == json.dumps(canonical)


def test_runtime_metadata_captures_lock_hash() -> None:
    root = Path(__file__).resolve().parents[2]
    metadata = capture_runtime_metadata(root)

    assert metadata.application_version == "0.5.0"
    assert metadata.uv_lock_sha256 == sha256_file(root / "uv.lock")
    assert metadata.captured_at.utcoffset().total_seconds() == 0


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is required for provenance test")
def test_runtime_metadata_hashes_dirty_tracked_and_untracked_content(tmp_path: Path) -> None:
    _git(tmp_path, "init")
    tracked = tmp_path / "tracked.py"
    tracked.write_text("VALUE = 1\n")
    _git(tmp_path, "add", "tracked.py")
    _git(
        tmp_path,
        "-c",
        "user.name=AutoLab Test",
        "-c",
        "user.email=autolab@example.invalid",
        "commit",
        "-m",
        "initial",
    )

    clean = capture_runtime_metadata(tmp_path)
    assert clean.git_commit is not None
    assert clean.git_dirty is False
    assert clean.source_tree_diff_sha256 is None

    tracked.write_text("VALUE = 2\n")
    first_dirty = capture_runtime_metadata(tmp_path)
    assert first_dirty.git_dirty is True
    assert first_dirty.source_tree_diff_sha256 is not None

    tracked.write_text("VALUE = 3\n")
    second_dirty = capture_runtime_metadata(tmp_path)
    assert second_dirty.source_tree_diff_sha256 != first_dirty.source_tree_diff_sha256

    untracked = tmp_path / "untracked.txt"
    untracked.write_text("first\n")
    with_untracked = capture_runtime_metadata(tmp_path)
    untracked.write_text("second\n")
    changed_untracked = capture_runtime_metadata(tmp_path)
    assert changed_untracked.source_tree_diff_sha256 != with_untracked.source_tree_diff_sha256


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        timeout=5,
    )
