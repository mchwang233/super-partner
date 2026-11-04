"""Opt-in command tools and hooks, executed by the existing Workspace sandbox only."""
from __future__ import annotations

import json
import re
import shlex

from .config import settings

RESERVED = {"Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "LS", "RecallHistory"}
EVENTS = {"before_tool", "after_tool"}


def definitions(workspace):
    if workspace.read_only or workspace.host:
        return []
    found = []
    for name, spec in settings.api_tool_extensions.items():
        if name in RESERVED or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", name):
            raise ValueError(f"Invalid extension tool name: {name}")
        command = spec.get("command")
        if not isinstance(command, list) or not command or any(not isinstance(s, str) or not s for s in command):
            raise ValueError(f"Extension {name} needs an argv command")
        schema = spec.get("parameters") or {"type": "object", "properties": {}, "additionalProperties": False}
        validate_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("Extension arguments must be objects")
        found.append({"name": name, "description": str(spec.get("description") or name), "parameters": schema})
    return found


def validate_schema(schema, depth=0):
    if depth > 8 or not isinstance(schema, dict):
        raise ValueError("Extension schema is invalid or too deep")
    allowed = {"type", "description", "properties", "required", "additionalProperties", "items", "enum"}
    if set(schema) - allowed or schema.get("type") not in {"object", "array", "string", "integer", "number", "boolean"}:
        raise ValueError("Unsupported extension schema; use primitive types, objects, arrays and enums")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], bool):
        raise ValueError("additionalProperties must be a boolean")
    for sub in (schema.get("properties") or {}).values():
        validate_schema(sub, depth + 1)
    if schema.get("type") == "array":
        validate_schema(schema.get("items"), depth + 1)


def validate(value, schema):
    kind = schema["type"]
    types = {"object": dict, "array": list, "string": str, "integer": int, "number": (int, float), "boolean": bool}
    if not isinstance(value, types[kind]) or (kind in {"integer", "number"} and isinstance(value, bool)):
        raise ValueError(f"Expected {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError("Value not in enum")
    if kind == "object":
        properties = schema.get("properties") or {}
        if any(key not in value for key in schema.get("required", [])):
            raise ValueError("Missing required argument")
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            raise ValueError("Unknown argument")
        for key in value.keys() & properties.keys():
            validate(value[key], properties[key])
    if kind == "array":
        for item in value:
            validate(item, schema["items"])


async def hook(workspace, event, payload):
    if workspace.read_only or workspace.host:
        return None
    for argv in settings.api_tool_hooks.get(event, []):
        if not isinstance(argv, list) or not argv or any(not isinstance(item, str) for item in argv):
            return f"Invalid {event} hook command"
        output, ok = await workspace.bash(shlex.join([*argv, json.dumps(payload, ensure_ascii=False)]), 60)
        if not ok:
            return f"{event} hook failed: {output}"
    return None


async def execute(workspace, name, args):
    definitions(workspace)  # validate configuration before running anything
    spec = settings.api_tool_extensions[name]
    try:
        if len(json.dumps(args).encode()) > 65536:
            raise ValueError("Extension arguments exceed 64 KiB")
        validate(args, spec.get("parameters") or {"type": "object", "properties": {}, "additionalProperties": False})
    except (ValueError, TypeError) as exc:
        return f"Error: {exc}", False
    return await workspace.bash(shlex.join([*spec["command"], json.dumps(args, ensure_ascii=False)]), 60)
