"""Recipe settings files: defaults, schema checks, re-reading, the folder, and what an agent may change."""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from busymeter import recipes as registry
from busymeter.recipes.settings import Field, Schema, SettingsError, SettingsFile, ascending_numbers, recipes_dir

SCHEMA = Schema({
    "count": Field("int", 3, "a count", min=1, max=10),
    "ratio": Field("float", 0.5, min=0, max=1),
    "on": Field("bool", True),
    "name": Field("str", "x", max_length=8),
    "level": Field("level", 2),
    "mode": Field("choice", "a", choices=("a", "b")),
    "steps": Field("list", [1, 2], check=ascending_numbers(3, 1, 100)),
    "secret": Field("str", "", secret=True),
    "where": Field("str", "", agent=False),
})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "recipes"


class SchemaTests(Base):
    def test_defaults_fill_missing_keys(self):
        values = SCHEMA.validate({"count": 5})
        self.assertEqual(5, values["count"])
        self.assertEqual(0.5, values["ratio"])
        self.assertEqual([1, 2], values["steps"])

    def test_types_and_ranges_name_the_key(self):
        bad = [({"count": 0}, "count: must be at least 1"), ({"count": 11}, "count: must be at most 10"),
               ({"count": "3"}, "count: must be a number"), ({"count": True}, "count: must be a number"),
               ({"count": 2.5}, "whole number"), ({"ratio": 2}, "ratio"), ({"on": 1}, "on: must be true or false"),
               ({"name": "123456789"}, "name: at most 8"), ({"level": 7}, "level: must be 0-6"),
               ({"mode": "c"}, "mode: must be one of a, b"), ({"steps": [2, 1]}, "steps: entries must go up"),
               ({"steps": []}, "steps: a list"), ({"steps": [1, 2, 3, 4]}, "steps: a list"),
               ({"nope": 1}, "nope: not a setting"), ([], "JSON object")]
        for data, text in bad:
            with self.subTest(data=data):
                with self.assertRaises(SettingsError) as ctx:
                    SCHEMA.validate(data)
                self.assertIn(text, str(ctx.exception))

    def test_comments_are_ignored_and_levels_normalised(self):
        values = SCHEMA.validate({"_comment": "hi", "level": "FLASH", "count": 4.0})
        self.assertEqual("flash", values["level"])
        self.assertEqual(4, values["count"])
        self.assertNotIn("_comment", values)

    def test_agent_changes(self):
        self.assertEqual({"count": 4}, SCHEMA.check_agent_changes({"count": 4}))
        for changes, text in [({"secret": "pw"}, "settings file"), ({"where": "C:/"}, "settings file"),
                              ({"nope": 1}, "not a setting"), ({"count": 99}, "at most"), ({}, "Pass the changes"),
                              ("count=4", "Pass the changes")]:
            with self.subTest(changes=changes):
                with self.assertRaises(SettingsError) as ctx:
                    SCHEMA.check_agent_changes(changes)
                self.assertIn(text, str(ctx.exception))

    def test_public_view_hides_secrets(self):
        view = SCHEMA.public_view(SCHEMA.validate({"secret": "hunter2"}))
        self.assertNotIn("hunter2", json.dumps(view))
        self.assertEqual("(set in the settings file)", view["secret"])
        described = SCHEMA.describe()
        self.assertNotIn("default", described["secret"])
        self.assertFalse(described["secret"]["agentCanSet"])
        self.assertEqual({"type": "int", "description": "a count", "default": 3, "min": 1, "max": 10},
                         described["count"])

    def test_secret_values_never_appear_in_errors(self):
        schema = Schema({"pw": Field("str", "", secret=True, max_length=4)})
        with self.assertRaises(SettingsError) as ctx:
            schema.validate({"pw": "toolongsecret"})
        self.assertNotIn("toolongsecret", str(ctx.exception))


class FileTests(Base):
    def test_ensure_writes_defaults_once(self):
        f = SettingsFile("demo", SCHEMA, self.dir, restrict=False)
        self.assertFalse(f.exists())
        self.assertEqual(SCHEMA.defaults(), f.ensure())
        data = json.loads(f.path.read_text(encoding="utf-8"))
        self.assertEqual(3, data["count"])
        f.update({"count": 7})
        self.assertEqual(7, SettingsFile("demo", SCHEMA, self.dir, restrict=False).ensure()["count"])

    def test_bad_files_are_errors_with_the_path(self):
        f = SettingsFile("demo", SCHEMA, self.dir, restrict=False)
        self.dir.mkdir(parents=True)
        for content, text in [("{not json", "isn't valid JSON"), ('{"count": 0}', "count: must be at least 1"),
                              ("[1]", "JSON object"), ("[" * 100000 + "]" * 100000, "")]:
            with self.subTest(content=content[:20]):
                f.path.write_text(content, encoding="utf-8")
                with self.assertRaises(SettingsError) as ctx:
                    f.load()
                self.assertIn(str(f.path), str(ctx.exception))
                self.assertIn(text, str(ctx.exception))

    def test_large_files_are_refused(self):
        f = SettingsFile("demo", SCHEMA, self.dir, restrict=False)
        self.dir.mkdir(parents=True)
        f.path.write_text('{"name": "' + "x" * 300000 + '"}', encoding="utf-8")
        with self.assertRaises(SettingsError):
            f.load()

    def test_changed_notices_edits(self):
        f = SettingsFile("demo", SCHEMA, self.dir, restrict=False)
        f.ensure()
        self.assertFalse(f.changed())
        time.sleep(0.02)
        f.path.write_text('{"count": 9}', encoding="utf-8")
        os.utime(f.path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
        self.assertTrue(f.changed())
        self.assertEqual(9, f.load()["count"])
        self.assertFalse(f.changed())

    def test_update_keeps_comments(self):
        f = SettingsFile("demo", SCHEMA, self.dir, restrict=False)
        self.dir.mkdir(parents=True)
        f.path.write_text('{"_note": "mine", "count": 2}', encoding="utf-8")
        f.update({"count": 5})
        data = json.loads(f.path.read_text(encoding="utf-8"))
        self.assertEqual("mine", data["_note"])
        self.assertEqual(5, data["count"])

    def test_folder_is_next_to_the_tokens_and_overridable(self):
        with mock.patch.dict(os.environ, {"BUSYMETER_TOKEN_DIR": str(Path(self.tmp.name) / "a" / "tokens")}):
            os.environ.pop("BUSYMETER_RECIPES_DIR", None)
            self.assertEqual(Path(self.tmp.name) / "a" / "recipes", recipes_dir())
        with mock.patch.dict(os.environ, {"BUSYMETER_RECIPES_DIR": str(self.dir)}):
            self.assertEqual(self.dir, recipes_dir())

    def test_folder_is_narrowed_like_the_token_folder(self):
        with mock.patch("busymeter.tokens.restrict_windows_dir") as narrow, mock.patch("os.chmod") as chmod:
            SettingsFile("demo", SCHEMA, self.dir).ensure()
        if sys.platform == "win32":
            narrow.assert_called_with(self.dir)
        else:
            self.assertTrue(chmod.called)


class GeneratedValueTests(Base):
    def test_a_missing_generated_token_is_written_back_and_stays(self):
        from busymeter.recipes.game_state import SCHEMA as GAME

        self.dir.mkdir(parents=True)
        path = self.dir / "game_state.json"
        path.write_text('{"_note": "mine", "port": 47830}', encoding="utf-8")
        first = SettingsFile("game_state", GAME, self.dir, restrict=False).load()
        second = SettingsFile("game_state", GAME, self.dir, restrict=False).load()
        self.assertEqual(first["token"], second["token"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(first["token"], saved["token"])
        self.assertEqual("mine", saved["_note"])
        self.assertEqual(47830, saved["port"])

    def test_a_read_only_folder_keeps_the_generated_token_for_this_run(self):
        import warnings

        from busymeter.recipes import settings as settings_module
        from busymeter.recipes.game_state import SCHEMA as GAME

        self.dir.mkdir(parents=True)
        path = self.dir / "game_state.json"
        path.write_text('{"port": 47832}', encoding="utf-8")
        f = SettingsFile("game_state", GAME, self.dir, restrict=False)
        with mock.patch.object(SettingsFile, "write", side_effect=PermissionError(13, "Access is denied")), \
                warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            first = f.load()
            second = f.load()
        self.assertGreaterEqual(len(first["token"]), 32)
        self.assertEqual(first["token"], second["token"])  # the same for this run
        warned = [w for w in caught if issubclass(w.category, settings_module.SettingsWarning)]
        self.assertEqual(1, len(warned))  # once
        self.assertIn(str(path), str(warned[0].message))
        self.assertNotIn(first["token"], str(warned[0].message))
        self.assertEqual('{"port": 47832}', path.read_text(encoding="utf-8"))  # untouched
        self.assertFalse(f.changed())

    def test_show_token_prints_the_token_the_runner_uses(self):
        import contextlib
        import io

        from busymeter.recipes import cli as recipes_cli
        from busymeter.recipes.game_state import SCHEMA as GAME

        self.dir.mkdir(parents=True)
        (self.dir / "game_state.json").write_text('{"port": 47831}', encoding="utf-8")
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"BUSYMETER_RECIPES_DIR": str(self.dir)}), \
                mock.patch("busymeter.tokens.restrict_windows_dir"), contextlib.redirect_stdout(out):
            self.assertEqual(0, recipes_cli.show_token("game_state"))
        runner_token = SettingsFile("game_state", GAME, self.dir, restrict=False).ensure()["token"]
        self.assertIn(runner_token, out.getvalue())


class BundledSchemaTests(unittest.TestCase):
    def test_every_bundled_recipe_has_valid_defaults_and_ids(self):
        for name in registry.names():
            cls = registry.get(name)
            with self.subTest(recipe=name):
                self.assertEqual(name, cls.name)
                self.assertRegex(cls.plugin_id, r"^com\.thebusymeter\.[a-z]+$")
                values = cls.schema.validate(cls.schema.defaults())
                decls = cls(values).signals()
                self.assertTrue(decls)
                self.assertTrue(cls.reads and cls.never_reads and cls.summary)

    def test_each_recipe_has_a_script_in_the_recipes_folder(self):
        folder = Path(__file__).resolve().parents[2] / "recipes"
        for name in registry.names():
            with self.subTest(recipe=name):
                text = (folder / f"{name}.py").read_text(encoding="utf-8")
                self.assertIn(f'main(["run", "{name}", *sys.argv[1:]])', text)
                self.assertIn(f"### `{name}`", (folder / "README.md").read_text(encoding="utf-8"))

    def test_only_bundled_names(self):
        for bad in ("../vibe_meter", "busymeter.recipes.vibe_meter", "os", "", None, "VIBE_METER"):
            with self.subTest(name=bad):
                with self.assertRaises(KeyError):
                    registry.get(bad)


if __name__ == "__main__":
    unittest.main()
