"""Recipe settings: one JSON file per recipe, checked against a small schema (defaults, types, ranges).

Files live in the per-user recipes folder, next to the token folder (see busymeter.tokens) and narrowed to the current
user the same way:

- Windows: ``%LOCALAPPDATA%\\busymeter-plugins\\recipes\\<recipe>.json``
- macOS/Linux: ``$XDG_CONFIG_HOME/busymeter-plugins/recipes/`` (else ``~/.config/...``)
- ``BUSYMETER_RECIPES_DIR`` overrides the folder.

A file holds only the keys a recipe knows. Keys starting with ``_`` are comments and ignored; any other unknown key, a
wrong type or a value out of range is an error that names the key. A missing key takes its default. Secret fields (the
OBS password, the game-state token) are never shown or accepted through the MCP server, and recipes never print them.
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from ..client import normalize_level
from ..tokens import default_token_dir, ensure_private_dir

MAX_FILE_BYTES = 256 * 1024


def recipes_dir() -> Path:
    override = os.environ.get("BUSYMETER_RECIPES_DIR")
    if override:
        return Path(override)
    return default_token_dir().parent / "recipes"


class SettingsWarning(UserWarning):
    """A settings file couldn't be updated (e.g. a read-only folder); the recipe still runs."""


class SettingsError(ValueError):
    """A settings file or change that doesn't fit the recipe's schema. The message names the key."""


@dataclass(frozen=True)
class Field:
    """One setting.

    kind: "int", "float", "bool", "str", "level" (0-6 or "flash"), "choice", "list" (custom ``check``).
    secret: never shown or accepted through MCP, never printed. agent: False = only the user edits it in the file.
    """

    kind: str
    default: Any = None
    description: str = ""
    min: float | None = None
    max: float | None = None
    choices: tuple[str, ...] = ()
    max_length: int = 256
    min_length: int = 0
    secret: bool = False
    agent: bool = True
    default_factory: Callable[[], Any] | None = None
    check: Callable[[str, Any], Any] | None = None  # for "list": returns the cleaned value or raises SettingsError

    def make_default(self) -> Any:
        if self.default_factory is not None:
            return self.default_factory()
        return copy.deepcopy(self.default)

    def validate(self, key: str, value: Any) -> Any:
        kind = self.kind
        if kind == "bool":
            if not isinstance(value, bool):
                raise SettingsError(f"{key}: must be true or false.")
            return value
        if kind in ("int", "float"):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise SettingsError(f"{key}: must be a number.")
            if kind == "int":
                if isinstance(value, float):
                    if not value.is_integer():
                        raise SettingsError(f"{key}: must be a whole number.")
                    value = int(value)
            else:
                value = float(value)
            if self.min is not None and value < self.min:
                raise SettingsError(f"{key}: must be at least {_num(self.min)} (got {_num(value)}).")
            if self.max is not None and value > self.max:
                raise SettingsError(f"{key}: must be at most {_num(self.max)} (got {_num(value)}).")
            return value
        if kind == "str":
            if not isinstance(value, str):
                raise SettingsError(f"{key}: must be text.")
            if len(value) > self.max_length:
                raise SettingsError(f"{key}: at most {self.max_length} characters.")
            if len(value) < self.min_length:
                raise SettingsError(f"{key}: at least {self.min_length} characters.")
            return value
        if kind == "level":
            try:
                return normalize_level(value)
            except ValueError:
                raise SettingsError(f"{key}: must be 0-6 (stars; 0 = lip only) or \"flash\".") from None
        if kind == "choice":
            if value not in self.choices:
                raise SettingsError(f"{key}: must be one of {', '.join(self.choices)}.")
            return value
        if kind == "list":
            if self.check is None:
                raise SettingsError(f"{key}: no check for this list.")
            return self.check(key, value)
        raise SettingsError(f"{key}: unknown kind {kind}.")

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"type": self.kind, "description": self.description}
        if not self.secret:
            out["default"] = self.make_default() if self.default_factory is None else "(made on first run)"
        if self.min is not None:
            out["min"] = _num(self.min)
        if self.max is not None:
            out["max"] = _num(self.max)
        if self.choices:
            out["choices"] = list(self.choices)
        if self.secret:
            out["secret"] = True
        if not self.agent or self.secret:
            out["agentCanSet"] = False
        return out


def _num(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def ascending_numbers(max_items: int, minimum: float = 0, maximum: float = 1e9) -> Callable[[str, Any], Any]:
    """A list check: 1..max_items numbers, strictly ascending, each in [minimum, maximum]."""

    def check(key: str, value: Any) -> list[float | int]:
        if not isinstance(value, list) or not 1 <= len(value) <= max_items:
            raise SettingsError(f"{key}: a list of 1-{max_items} numbers, lowest first.")
        out: list[float | int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, (int, float)):
                raise SettingsError(f"{key}: every entry must be a number.")
            if not minimum <= item <= maximum:
                raise SettingsError(f"{key}: entries must be {_num(minimum)}-{_num(maximum)}.")
            if out and item <= out[-1]:
                raise SettingsError(f"{key}: entries must go up (each larger than the one before).")
            out.append(_num(item))
        return out

    return check


class Schema:
    def __init__(self, fields: Mapping[str, Field]):
        self.fields = dict(fields)

    def defaults(self) -> dict[str, Any]:
        return {key: f.make_default() for key, f in self.fields.items()}

    def validate(self, data: Any, *, fill: bool = True) -> dict[str, Any]:
        """A full settings dict from ``data`` (missing keys take defaults when ``fill``). Raises SettingsError."""
        if not isinstance(data, dict):
            raise SettingsError("The settings file must hold a JSON object.")
        out: dict[str, Any] = {}
        for key, value in data.items():
            if not isinstance(key, str):
                raise SettingsError("Keys must be text.")
            if key.startswith("_"):
                continue  # a comment
            spec = self.fields.get(key)
            if spec is None:
                raise SettingsError(f"{key}: not a setting of this recipe (known: {', '.join(self.fields)}).")
            out[key] = spec.validate(key, value)
        if fill:
            for key, spec in self.fields.items():
                if key not in out:
                    out[key] = spec.make_default()
        return out

    def check_agent_changes(self, changes: Any) -> dict[str, Any]:
        """Changes an agent (MCP) may make: known, non-secret, agent-settable keys, each valid on its own."""
        if not isinstance(changes, dict) or not changes:
            raise SettingsError("Pass the changes as an object, e.g. {\"threshold\": 5}.")
        out: dict[str, Any] = {}
        for key, value in changes.items():
            spec = self.fields.get(key) if isinstance(key, str) else None
            if spec is None:
                raise SettingsError(f"{key}: not a setting of this recipe (known: {', '.join(self.fields)}).")
            if spec.secret or not spec.agent:
                raise SettingsError(f"{key}: can't be set through the assistant; set it in the settings file.")
            out[key] = spec.validate(key, value)
        return out

    def public_view(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """Values safe to show an agent: secrets become whether they're set."""
        out: dict[str, Any] = {}
        for key, spec in self.fields.items():
            if spec.secret:
                out[key] = "(set in the settings file)" if values.get(key) else "(not set; set it in the settings file)"
            else:
                out[key] = copy.deepcopy(values.get(key))
        return out

    def describe(self) -> dict[str, Any]:
        return {key: f.describe() for key, f in self.fields.items()}


class SettingsFile:
    """One recipe's settings file: create with defaults, load and validate, notice changes."""

    def __init__(self, name: str, schema: Schema, directory: Path | str | None = None, restrict: bool = True):
        self.name = name
        self.schema = schema
        self.directory = Path(directory) if directory is not None else recipes_dir()
        self.restrict = restrict
        self._stamp: tuple[int, int] | None = None
        self._generated: dict[str, Any] = {}  # generated values that couldn't be written back
        self._warned = False

    @property
    def path(self) -> Path:
        return self.directory / f"{self.name}.json"

    def exists(self) -> bool:
        return self.path.is_file()

    def ensure(self) -> dict[str, Any]:
        """Load the file, or write the defaults first when there is none."""
        if not self.exists():
            self.write(self.schema.defaults())
        return self.load()

    def load(self) -> dict[str, Any]:
        """Read and validate. Raises SettingsError (with the file's path) on any problem."""
        try:
            stat = self.path.stat()
            if stat.st_size > MAX_FILE_BYTES:
                raise SettingsError(f"{self.path} is larger than {MAX_FILE_BYTES // 1024} KB.")
            raw = self.path.read_bytes()
        except FileNotFoundError:
            raise SettingsError(f"No settings file at {self.path}.") from None
        except OSError as e:
            raise SettingsError(f"Can't read {self.path}: {e.strerror or e}.") from None
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except (ValueError, RecursionError) as e:
            raise SettingsError(f"{self.path} isn't valid JSON ({_short(e)}).") from None
        try:
            values = self.schema.validate(data)
        except SettingsError as e:
            raise SettingsError(f"{self.path}: {e}") from None
        made = [k for k, f in self.schema.fields.items() if f.default_factory is not None and k not in data]
        if made:
            # A generated value (the game-state token) must stay the same from run to run: keep it in the file.
            if self._generated:
                values.update(self._generated)  # couldn't be saved earlier: the same value for this run
            try:
                self.write(values)
                stat = self.path.stat()
                self._generated = {}
            except OSError as e:
                self._generated = {k: values[k] for k in made}
                if not self._warned:
                    self._warned = True
                    warnings.warn(f"Couldn't save the generated {', '.join(made)} into {self.path} "
                                  f"({e.strerror or e}); it is used for this run only and changes next time.",
                                  SettingsWarning, stacklevel=2)
        self._stamp = (stat.st_mtime_ns, stat.st_size)
        return values

    def changed(self) -> bool:
        """True when the file's time or size differs from the last load (or it appeared/disappeared)."""
        try:
            stat = self.path.stat()
            stamp: tuple[int, int] | None = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            stamp = None
        return stamp != self._stamp

    def mark_seen(self) -> None:
        """Treat the file as it is now as seen (after a load that failed), so changed() waits for the next edit."""
        try:
            stat = self.path.stat()
            self._stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError:
            self._stamp = None

    def raw(self) -> dict[str, Any]:
        """The file's own JSON object (keeps comments); {} when missing or broken."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError, RecursionError):
            return {}
        return data if isinstance(data, dict) else {}

    def write(self, values: Mapping[str, Any]) -> None:
        """Validate and write atomically (folder narrowed to the current user first)."""
        cleaned = self.schema.validate(dict(values))
        comments = {k: v for k, v in self.raw().items() if isinstance(k, str) and k.startswith("_")}
        ensure_private_dir(self.directory, self.restrict)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({**comments, **cleaned}, f, indent=2)
                f.write("\n")
            if sys.platform != "win32":
                os.chmod(tmp, 0o600)
            _replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def update(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Apply changes on top of the current file (or the defaults) and write. Returns the new values."""
        try:
            current = self.load()
        except SettingsError:
            if self.exists():
                raise
            current = self.schema.defaults()
        merged = {**current, **changes}
        self.write(merged)
        return self.load()

    def delete(self) -> bool:
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False


def _replace(src: str, dst: Path, attempts: int = 20) -> None:
    """os.replace, retried briefly: on Windows a reader holding the file open makes it fail for a moment."""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.05)


def _short(e: Exception) -> str:
    text = str(e)
    return text if len(text) <= 120 else text[:117] + "..."
