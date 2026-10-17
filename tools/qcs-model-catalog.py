#!/usr/bin/env python3
"""Read the application model lock for development and package verification."""
from __future__ import annotations

import json
from pathlib import Path
import re

SOURCE = Path(__file__).resolve().parents[1]


def catalog():
    document = json.loads((SOURCE / "config/qcs-models.lock.json").read_text(encoding="utf-8"))
    if document.get("schemaVersion") != 1:
        raise ValueError("Unsupported model catalog schema")
    models = {}
    for model in document.get("models", []):
        identifier = model.get("id", "")
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", identifier) or identifier in models:
            raise ValueError("Invalid or duplicate model identifier")
        if type(model.get("dimensions")) is not int or model["dimensions"] < 1:
            raise ValueError("Invalid model embedding dimensions")
        artifacts = model.get("artifacts", [])
        names = set()
        for artifact in artifacts:
            name = artifact.get("name", "")
            if (not name or Path(name).name != name or "/" in name or "\\" in name or name in names
                    or type(artifact.get("size")) is not int or artifact["size"] < 1
                    or not re.fullmatch(r"[0-9a-f]{64}", artifact.get("sha256", ""))):
                raise ValueError("Invalid or duplicate locked model artifact")
            names.add(name)
        if not artifacts or {model.get("textFile"), model.get("mediaFile")} != names:
            raise ValueError("Model lock must bind its text and projection artifacts")
        models[identifier] = model
    if document.get("defaultModelId") not in models:
        raise ValueError("Model catalog has no valid default")
    return document["defaultModelId"], models


def selected(identifier=None):
    default, models = catalog()
    identifier = identifier or default
    if identifier not in models:
        raise ValueError("Unknown model preset: " + identifier)
    return models[identifier]


def artifacts(identifier=None):
    return {row["name"]: (row["size"], row["sha256"]) for row in selected(identifier)["artifacts"]}
