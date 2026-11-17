"""Load and validate catalog documents from a directory of YAML files.

Compiling a profile produces an *install plan*: a self-contained JSON document with everything
the in-VM installer needs, so the VM never reads the catalog or calls a cloud API to decide what
to do. Composition lives in `patchgate.plan` so the cloud resolver shares it without YAML.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from patchgate.plan import (
    API_VERSION,
    KINDS,
    LEVELS,
    MAX_ENCODED_PLAN_CHARS,
    PLAN_PREFIX,
    CatalogError,
    Document,
    Library,
    decode_plan,
    digest,
    encode_plan,
)
from patchgate.schema import SCHEMA_DIR, validate_document

__all__ = [
    "API_VERSION",
    "KINDS",
    "LEVELS",
    "MAX_ENCODED_PLAN_CHARS",
    "PLAN_PREFIX",
    "SCHEMA_DIR",
    "Catalog",
    "CatalogError",
    "Document",
    "decode_plan",
    "digest",
    "encode_plan",
    "validate_document",
]


class Catalog(Library):
    @classmethod
    def load(cls, root: Path) -> Catalog:
        """Load and fully validate a catalog directory. Raises CatalogError on any problem."""
        catalog = cls()
        errors: list[str] = []
        for path in sorted(root.rglob("*.y*ml")):
            source = str(path.relative_to(root))
            try:
                doc = yaml.safe_load(path.read_text())
            except yaml.YAMLError as exc:
                errors.append(f"{source}: invalid YAML: {exc}")
                continue
            doc_errors = validate_document(doc, source)
            if doc_errors:
                errors.extend(doc_errors)
                continue
            name = doc["metadata"]["name"]
            if path.stem != name:
                errors.append(f"{source}: file name must match metadata.name ({name})")
            if name in catalog.bucket(doc["kind"]):
                errors.append(f"{source}: duplicate {doc['kind']} '{name}'")
            catalog.add(doc)
        errors.extend(catalog.reference_errors())
        if errors:
            raise CatalogError(errors)
        return catalog

    def documents(self) -> list[Document]:
        """Every document, ordered by kind and name (used to seed the configuration store)."""
        return [doc for kind in KINDS for _, doc in sorted(self.bucket(kind).items())]
