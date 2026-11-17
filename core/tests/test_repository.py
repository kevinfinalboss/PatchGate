"""Repository-level sanity tests. Replaced by real unit tests as core/lib grows."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_core_has_no_cloud_sdk_imports() -> None:
    offenders = [
        str(path.relative_to(ROOT))
        for path in (ROOT / "core").rglob("*.py")
        for line in path.read_text().splitlines()
        if line.lstrip().startswith(
            ("import boto3", "from boto3", "import botocore", "import azure")
        )
    ]
    assert offenders == []


def test_every_adr_is_indexed() -> None:
    adr_dir = ROOT / "docs" / "adr"
    index = (adr_dir / "README.md").read_text()
    adrs = [
        p.name for p in adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md") if not p.name.startswith("0000")
    ]
    assert adrs, "expected at least one ADR"
    missing = [name for name in adrs if name not in index]
    assert missing == []
