"""Saved API keys, and which one is active for each provider.

`.env` always holds the active key, so the CLI and the web backend agree without
either knowing about this module. `data/keys.json` holds the saved alternatives.
Both files are plain text on this machine and are gitignored.

Full key values never leave this module through `listing()`: callers get a
stable id derived from the key, a label, and a masked form.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from pathlib import Path

MIN_KEY_LENGTH = 20
MAX_LABEL_LENGTH = 40
IMPORTED_LABEL = "From .env"


class KeyStoreError(ValueError):
    """A request the store refuses, with a message safe to show the user."""


def key_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def mask(value: str) -> str:
    return f"{value[:4]}…{value[-4:]}"


def _atomic_write(path: Path, text: str) -> None:
    """Replace a file in one step, so a crash cannot leave half a secrets file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


class KeyStore:
    def __init__(self, env_path: Path, store_path: Path, env_keys: dict[str, str]):
        self.env_path = env_path
        self.store_path = store_path
        self.env_keys = env_keys
        self._lock = threading.RLock()

    # ---- files ---------------------------------------------------------

    def _read(self) -> dict[str, list[dict]]:
        try:
            data = json.loads(self.store_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            # Never overwrite a file we could not understand: it may hold keys.
            raise KeyStoreError("data/keys.json is damaged; fix or remove it and try again") from exc
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict[str, list[dict]]) -> None:
        _atomic_write(self.store_path, json.dumps(data, indent=2) + "\n")

    def _set_active(self, provider: str, value: str) -> None:
        env_key = self.env_keys[provider]
        try:
            lines = self.env_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []

        replaced = False
        for index, line in enumerate(lines):
            if line.lstrip("﻿").startswith(f"{env_key}="):
                lines[index] = f"{env_key}={value}"
                replaced = True
        if not replaced:
            lines.append(f"{env_key}={value}")

        _atomic_write(self.env_path, "\n".join(lines) + "\n")
        if value:
            os.environ[env_key] = value
        else:
            os.environ.pop(env_key, None)

    # ---- helpers -------------------------------------------------------

    def _require_provider(self, provider: str) -> None:
        if provider not in self.env_keys:
            raise KeyStoreError(f"provider '{provider}' does not use an API key")

    def _active_value(self, provider: str) -> str:
        return os.environ.get(self.env_keys[provider], "").strip()

    def _adopt_active(self, data: dict, provider: str) -> list[dict]:
        """Save the key currently in .env, so switching away cannot lose it."""
        saved = data.setdefault(provider, [])
        active = self._active_value(provider)
        if active and all(entry["value"] != active for entry in saved):
            saved.append({"label": IMPORTED_LABEL, "value": active})
        return saved

    @staticmethod
    def _clean_value(value: str) -> str:
        cleaned = (value or "").strip()
        if len(cleaned) < MIN_KEY_LENGTH:
            raise KeyStoreError("that is too short to be an API key")
        if any(ch.isspace() or ch in "#\"'" or not ch.isprintable() for ch in cleaned):
            raise KeyStoreError("an API key cannot contain spaces, quotes or # characters")
        return cleaned

    @staticmethod
    def _find(saved: list[dict], wanted: str) -> dict:
        for entry in saved:
            if key_id(entry["value"]) == wanted:
                return entry
        raise KeyStoreError("that saved key no longer exists")

    # ---- operations ----------------------------------------------------

    def listing(self) -> dict[str, list[dict]]:
        """Saved keys per provider, masked. Reading never writes."""
        with self._lock:
            data = self._read()
            result: dict[str, list[dict]] = {}
            for provider in self.env_keys:
                active = self._active_value(provider)
                entries = list(data.get(provider, []))
                if active and all(entry["value"] != active for entry in entries):
                    entries.append({"label": IMPORTED_LABEL, "value": active})
                result[provider] = [
                    {
                        "id": key_id(entry["value"]),
                        "label": entry["label"],
                        "masked": mask(entry["value"]),
                        "active": entry["value"] == active,
                    }
                    for entry in entries
                ]
            return result

    def add(self, provider: str, label: str, value: str, activate: bool = True) -> str:
        self._require_provider(provider)
        cleaned = self._clean_value(value)
        name = (label or "").strip()[:MAX_LABEL_LENGTH] or f"Key ending {cleaned[-4:]}"
        with self._lock:
            data = self._read()
            saved = self._adopt_active(data, provider)
            existing = next((entry for entry in saved if entry["value"] == cleaned), None)
            if existing:
                existing["label"] = name
            else:
                saved.append({"label": name, "value": cleaned})
            self._write(data)
            if activate:
                self._set_active(provider, cleaned)
        return key_id(cleaned)

    def activate(self, provider: str, wanted: str) -> None:
        self._require_provider(provider)
        with self._lock:
            data = self._read()
            saved = self._adopt_active(data, provider)
            entry = self._find(saved, wanted)
            self._write(data)
            self._set_active(provider, entry["value"])

    def remove(self, provider: str, wanted: str) -> None:
        """Forget a key. Removing the active one leaves the provider with none."""
        self._require_provider(provider)
        with self._lock:
            data = self._read()
            saved = self._adopt_active(data, provider)
            entry = self._find(saved, wanted)
            was_active = entry["value"] == self._active_value(provider)
            saved.remove(entry)
            self._write(data)
            if was_active:
                self._set_active(provider, "")

    def value_of(self, provider: str, wanted: str) -> str:
        self._require_provider(provider)
        with self._lock:
            data = self._read()
            saved = list(data.get(provider, []))
            active = self._active_value(provider)
            if active and all(entry["value"] != active for entry in saved):
                saved.append({"label": IMPORTED_LABEL, "value": active})
            return self._find(saved, wanted)["value"]
