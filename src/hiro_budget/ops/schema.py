"""JSON schemas of the argument and result models for MCP: self-contained (no ``$ref``,
some clients do not follow them) and without the command line hints."""

from __future__ import annotations

import copy
from typing import Any

from pydantic import BaseModel

# keys of json_schema_extra that only the command line uses (ops.positional / ops.flag)
_CLI_KEYS = frozenset({"cli", "flags", "nargs"})


def _resolve(node: Any, defs: dict[str, Any]) -> Any:
    if isinstance(node, list):
        return [_resolve(x, defs) for x in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        target = copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]])
        extra = {k: v for k, v in node.items() if k != "$ref"}
        return _resolve({**target, **extra}, defs)
    return {k: _resolve(v, defs) for k, v in node.items()}


def _clean(node: Any, in_properties: bool = False) -> Any:
    """Drop the generated ``title`` keywords and the command line keys; the keys of a
    ``properties`` mapping are field names and stay as they are."""
    if isinstance(node, list):
        return [_clean(x) for x in node]
    if not isinstance(node, dict):
        return node
    if in_properties:
        return {k: _clean(v) for k, v in node.items()}
    return {
        k: _clean(v, in_properties=(k == "properties"))
        for k, v in node.items()
        if k not in _CLI_KEYS and k != "title"
    }


def inline_schema(model: type[BaseModel], mode: str = "validation") -> dict[str, Any]:
    """The model's JSON schema with every ``$ref`` replaced by its definition."""
    schema = model.model_json_schema(mode=mode)  # type: ignore[arg-type]
    defs = schema.pop("$defs", {})
    schema = _clean(_resolve(schema, defs))
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    if mode == "validation":
        schema["additionalProperties"] = False
    return schema
