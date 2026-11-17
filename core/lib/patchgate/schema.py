"""JSON Schema validation of catalog documents."""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError, best_match

from patchgate.plan import KINDS

# Bundled next to the package in deployment artifacts; repository layout otherwise.
_CANDIDATES = (
    Path(__file__).resolve().parent / "schemas",
    Path(__file__).resolve().parents[2] / "schemas",
)
SCHEMA_DIR = next((p for p in _CANDIDATES if p.is_dir()), _CANDIDATES[-1])


@cache
def _validator(kind: str) -> Draft202012Validator:
    schema = json.loads((SCHEMA_DIR / f"{kind.lower()}.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validate_document(doc: Any, source: str = "<memory>") -> list[str]:
    """Return schema errors for one document (empty list when valid)."""
    if not isinstance(doc, dict) or doc.get("kind") not in KINDS:
        return [f"{source}: kind must be one of {', '.join(KINDS)}"]
    validator = _validator(str(doc["kind"]))
    errors = sorted(validator.iter_errors(doc), key=lambda e: list(e.absolute_path))
    return [f"{source}: {_describe(err)}" for err in errors]


def _describe(err: ValidationError) -> str:
    """Explain an error; for oneOf/anyOf, report errors of the branch matching `method`."""
    if err.context:
        branches: dict[Any, list[ValidationError]] = {}
        for sub in err.context:
            branches.setdefault(sub.schema_path[0], []).append(sub)
        matching = [
            subs
            for subs in branches.values()
            if not any(list(sub.relative_path) == ["method"] for sub in subs)
        ]
        candidates = matching[0] if len(matching) == 1 else list(err.context)
        err = best_match(candidates)
    return f"{'/'.join(map(str, err.absolute_path)) or '<root>'}: {err.message}"
