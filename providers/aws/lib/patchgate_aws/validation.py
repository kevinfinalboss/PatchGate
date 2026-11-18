"""Validation of every document the configuration table holds: core kinds use the core schemas,
provider kinds (Account) use the provider's own schemas."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from patchgate.schema import validate_document as validate_core

# Bundled next to the package in the Lambda layer; repository layout otherwise.
_CANDIDATES = (
    Path(__file__).resolve().parent / "schemas",
    Path(__file__).resolve().parents[2] / "schemas",
)
SCHEMA_DIR = next((p for p in _CANDIDATES if p.is_dir()), _CANDIDATES[-1])
PROVIDER_SCHEMAS = {"Account": "account.schema.json"}


@cache
def _validator(kind: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / PROVIDER_SCHEMAS[kind]).read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate(doc: Any, source: str = "<memory>") -> list[str]:
    if isinstance(doc, dict) and doc.get("kind") in PROVIDER_SCHEMAS:
        errors = sorted(_validator(doc["kind"]).iter_errors(doc), key=lambda e: list(e.path))
        return [f"{source}: {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
                for e in errors]  # fmt: skip
    return list(validate_core(doc, source))
