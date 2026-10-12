"""The vibe meter with synthetic transcript files in a temp folder (never the real ~/.claude)."""

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from busymeter.recipes.settings import SettingsError
from busymeter.recipes.vibe_meter import SCHEMA, TranscriptWatcher, VibeMeter, usage_from_line

SECRET = "TOP-SECRET-PROMPT-TEXT"


def assistant(msg_id="msg_1", inp=10, out=100, cc=1000, cr=50000, content=SECRET):
    return json.dumps({
        "parentUuid": "p", "type": "assistant", "sessionId": "s",
        "message": {"id": msg_id, "role": "assistant", "content": [{"type": "text", "text": content}],
                    "usage": {"input_tokens": inp, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr,
                              "output_tokens": out, "output_tokens_details": {"thinking_tokens": 5},
                              "server_tool_use": {"web_search_requests": 0}}},
    })


def user(content=SECRET):
    return json.dumps({"type": "user", "message": {"role": "user", "content": content},
                       "toolUseResult": {"usage": {"input_tokens": 1, "output_tokens": 99999}}})


class WallClock:
    def __init__(self):
        self.now = 2_000_000_000.0

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "projects"
        self.wall = WallClock()

    def file(self, project="proj-a", session="s1"):
        path = self.root / project / f"{session}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.touch(path)
        return path

    def touch(self, path, age=0.0):
        t = self.wall.now - age
        os.utime(path, (t, t))

    def append(self, path, *lines, end="\n", age=0.0):
        with open(path, "a", encoding="utf-8", newline="") as f:
            f.write("".join(line + end for line in lines))
        self.touch(path, age)


class LineTests(unittest.TestCase):
    def test_only_the_usage_numbers_and_id(self):
        self.assertEqual(("msg_1", (10, 100, 1000, 50000)), usage_from_line(assistant().encode()))

    def test_user_lines_and_text_that_looks_like_usage_are_ignored(self):
        self.assertIsNone(usage_from_line(user().encode()))
        fake = assistant(content='"usage":{"output_tokens":999999}').encode()
        self.assertEqual((10, 100, 1000, 50000), usage_from_line(fake)[1])  # the escaped text never matches
        no_usage = json.dumps({"type": "assistant", "message": {"content": '"usage":{"output_tokens":5}'}}).encode()
        self.assertIsNone(usage_from_line(no_usage))

    def test_the_rest_of_the_line_is_never_decoded(self):
        line = assistant().encode().replace(SECRET.encode(), b"broken \\q escape and {{{ not json")
        self.assertEqual((10, 100, 1000, 50000), usage_from_line(line)[1])

    def test_usage_and_type_inside_tool_input_never_count(self):
        # The tool call's input comes first and holds both a "usage" with token counts and "type": "user"; the real
        # message.usage comes after it, and the top-level type comes last (as in real transcripts).
        line = ('{"parentUuid":"p","message":{"id":"msg_real","content":[{"type":"tool_use","name":"x","input":'
                '{"type":"user","usage":{"input_tokens":7,"output_tokens":99999},"note":"\\"usage\\":{"}}],'
                '"stop_reason":"tool_use","usage":{"input_tokens":3,"output_tokens":4,'
                '"cache_creation_input_tokens":5,"cache_read_input_tokens":6,"server_tool_use":{"x":0}}},'
                '"type":"assistant","uuid":"u"}').encode()
        self.assertEqual(("msg_real", (3, 4, 5, 6)), usage_from_line(line))

    def test_a_user_line_with_assistant_looking_content_is_ignored(self):
        line = json.dumps({"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "content": {"type": "assistant", "usage": {"output_tokens": 5}}}],
            "usage": {"input_tokens": 1, "output_tokens": 2}}}).encode()
        self.assertIsNone(usage_from_line(line))
        nested = json.dumps({"type": "progress", "data": {"message": {"type": "assistant", "message": {
            "usage": {"input_tokens": 1, "output_tokens": 2}}}}}).encode()
        self.assertIsNone(usage_from_line(nested))  # only the top level counts (sub-agent progress lines)

    def test_malformed_lines_are_skipped(self):
        for line in (b'{"type":"assistant","message":{"usage":{"output_tokens":1}', b"[1,2]", b"",
                     b'{"type":"assistant","message":"usage"}', b'{"type":"assistant" "message":{}}'):
            with self.subTest(line=line):
                self.assertIsNone(usage_from_line(line))

    def test_large_lines_are_quick(self):
        import time as _time

        line = assistant(content="word \\\" " * 200_000).encode()
        start = _time.perf_counter()
        self.assertEqual((10, 100, 1000, 50000), usage_from_line(line)[1])
        self.assertLess(_time.perf_counter() - start, 1.0)

    def test_a_tool_input_named_usage_is_skipped(self):
        line = json.dumps({"type": "assistant", "message": {
            "content": [{"type": "tool_use", "input": {"usage": {"mode": "x"}}}],
            "usage": {"input_tokens": 1, "output_tokens": 2}}}).encode()
        self.assertEqual((1, 2, 0, 0), usage_from_line(line)[1])


class WatcherTests(Base):
    def test_history_is_not_counted_new_lines_are(self):
        path = self.file()
        self.append(path, assistant("msg_old"))
        watcher = TranscriptWatcher(self.root, self.wall)
        self.assertEqual((1, []), watcher.scan(180))
        before = watcher.bytes_read
        line = assistant("msg_new")
        self.append(path, line)
        sessions, deltas = watcher.scan(180)
        self.assertEqual((1, [(10, 100, 1000, 50000)]), (sessions, deltas))
        self.assertEqual(len(line) + 1, watcher.bytes_read - before)  # only the appended bytes were read
        self.assertEqual((1, []), watcher.scan(180))

    def test_partial_lines_wait_for_their_end(self):
        path = self.file()
        watcher = TranscriptWatcher(self.root, self.wall)
        watcher.scan(180)
        line = assistant("msg_2")
        self.append(path, line[:40], end="")
        self.assertEqual([], watcher.scan(180)[1])
        self.append(path, line[40:])
        self.assertEqual([(10, 100, 1000, 50000)], watcher.scan(180)[1])

    def test_a_message_split_over_lines_counts_once(self):
        path = self.file()
        watcher = TranscriptWatcher(self.root, self.wall)
        watcher.scan(180)
        self.append(path, assistant("msg_3"), assistant("msg_3"), assistant("msg_3", out=150))
        self.assertEqual([(10, 100, 1000, 50000), (0, 50, 0, 0)], watcher.scan(180)[1])

    def test_active_sessions_by_modification_time(self):
        a, b, c = self.file("p1", "a"), self.file("p1", "b"), self.file("p2", "c")
        self.touch(c, age=400)
        watcher = TranscriptWatcher(self.root, self.wall)
        self.assertEqual(2, watcher.scan(180)[0])
        self.touch(a, age=200)
        self.assertEqual(1, watcher.scan(180)[0])
        (self.root / "p1" / "deeper").mkdir()
        (self.root / "p1" / "deeper" / "x.jsonl").write_text("{}\n")
        (self.root / "notes.jsonl").write_text("{}\n")
        self.assertEqual(1, watcher.scan(180)[0])  # only <projects>/<project>/<session>.jsonl

    def test_a_file_waking_up_is_read_from_where_it_was(self):
        path = self.file()
        self.append(path, assistant("msg_a"))
        watcher = TranscriptWatcher(self.root, self.wall)
        watcher.scan(180)
        self.wall.now += 600  # idle: forgotten
        self.assertEqual((0, []), watcher.scan(180))
        self.append(path, assistant("msg_b", out=7))
        self.assertEqual([(10, 7, 1000, 50000)], watcher.scan(180)[1])  # not msg_a again

    def test_a_new_session_is_read_from_the_start(self):
        watcher = TranscriptWatcher(self.root, self.wall)
        watcher.scan(180)
        path = self.file("p", "new")
        self.append(path, assistant("msg_n"))
        self.assertEqual((1, [(10, 100, 1000, 50000)]), watcher.scan(180))

    def test_a_shrunk_or_replaced_file_is_not_counted_again(self):
        path = self.file()
        watcher = TranscriptWatcher(self.root, self.wall)
        watcher.scan(180)
        self.append(path, assistant("msg_1"), assistant("msg_2"))
        watcher.scan(180)
        path.write_text(assistant("msg_1") + "\n", encoding="utf-8")
        self.touch(path)
        self.assertEqual([], watcher.scan(180)[1])
        self.append(path, assistant("msg_9", out=1))
        self.assertEqual([(10, 1, 1000, 50000)], watcher.scan(180)[1])

    def test_over_long_lines_are_skipped(self):
        path = self.file()
        watcher = TranscriptWatcher(self.root, self.wall)
        watcher.scan(180)
        self.append(path, assistant("msg_big", content="x" * (3 * 1024 * 1024)), assistant("msg_ok", out=3))
        self.assertEqual([(10, 3, 1000, 50000)], watcher.scan(180)[1])

    def test_files_are_never_written(self):
        paths = [self.file("p", f"s{i}") for i in range(3)]
        for p in paths:
            self.append(p, assistant(f"msg_{p.stem}"), user())
        before = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns, p.stat().st_size)
                  for p in paths}
        watcher = TranscriptWatcher(self.root, self.wall)
        for _ in range(3):
            watcher.scan(180)
        after = {p: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns, p.stat().st_size)
                 for p in paths}
        self.assertEqual(before, after)
        self.assertEqual(sorted(paths), sorted(Path(p) for p in Path(self.root).glob("*/*.jsonl")))

    def test_missing_folder_is_fine(self):
        self.assertEqual((0, []), TranscriptWatcher(self.root / "nope", self.wall).scan(180))


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class VibeMeterTests(Base):
    def meter(self, **settings):
        values = SCHEMA.validate({"projects_dir": str(self.root), "scan_seconds": 1, **settings})
        self.clock = Clock()
        self.messages = []
        recipe = VibeMeter(values, clock=self.clock, say=self.messages.append, wall_clock=self.wall)
        recipe.start()
        return recipe

    def tick(self, recipe, seconds=1.0):
        self.clock.now += seconds
        self.wall.now += seconds
        return recipe.poll()

    def sessions(self, n):
        paths = [self.file("p", f"s{i}") for i in range(n)]
        for p in paths:
            self.touch(p)
        return paths

    def test_signals_and_suggested_levels(self):
        recipe = self.meter()
        decls = recipe.signals()
        self.assertEqual([f"vibe-{n}" for n in range(1, 7)] + ["vibe-alert"], [d["name"] for d in decls])
        self.assertEqual([1, 2, 3, 4, 5, 6, "flash"], [d["suggest"] for d in decls])
        self.assertNotIn("suggest", self.meter(star_levels=False).signals()[0])

    def test_sessions_drive_stars_capped_at_six(self):
        recipe = self.meter(burst_sessions=0)
        self.assertEqual([], recipe.poll())
        self.sessions(2)
        self.assertEqual(["vibe-2"], self.tick(recipe))
        self.sessions(9)
        self.assertEqual(["vibe-6"], self.tick(recipe))

    def test_custom_session_thresholds(self):
        recipe = self.meter(session_thresholds=[1, 3, 5], burst_sessions=0)
        self.sessions(4)
        self.assertEqual(["vibe-2"], self.tick(recipe))

    def test_tokens_per_minute_driver(self):
        recipe = self.meter(driver="tokens", token_thresholds=[1000, 5000, 20000], burst_sessions=0,
                            burst_tokens_per_minute=0)
        (path,) = self.sessions(1)
        recipe.poll()
        self.append(path, assistant("msg_1", inp=0, out=2000, cc=4000, cr=10**6))  # 6000, cache reads not counted
        self.assertEqual(["vibe-2"], self.tick(recipe))
        self.assertEqual(6000, recipe.rate)
        self.wall.now += 61
        self.clock.now += 61
        self.touch(path)
        self.assertEqual([], recipe.poll())  # out of the 60 s window

    def test_cache_reads_can_be_counted(self):
        recipe = self.meter(driver="tokens", count_cache_reads=True, burst_tokens_per_minute=0, burst_sessions=0)
        (path,) = self.sessions(1)
        recipe.poll()
        self.append(path, assistant("msg_1", inp=0, out=0, cc=0, cr=60000))
        self.tick(recipe)
        self.assertEqual(60000, recipe.rate)

    def test_vibe_alert_on_a_burst_is_held_then_cools_down(self):
        recipe = self.meter(burst_sessions=4, alert_seconds=20, alert_cooldown_seconds=300)
        self.sessions(4)
        self.assertEqual(["vibe-4", "vibe-alert"], self.tick(recipe))
        self.assertEqual(["vibe-4", "vibe-alert"], self.tick(recipe, 19))
        self.assertEqual(["vibe-4"], self.tick(recipe, 2))  # held ~20 s, not for the whole burst
        for p in (self.root / "p").glob("*.jsonl"):
            self.touch(p, age=400)
        self.assertEqual([], self.tick(recipe))
        self.sessions(4)
        self.assertEqual(["vibe-4"], self.tick(recipe))  # within the cooldown: no new alert
        for p in (self.root / "p").glob("*.jsonl"):
            self.touch(p, age=400)
        self.tick(recipe, 300)
        self.sessions(5)
        self.assertEqual(["vibe-5", "vibe-alert"], self.tick(recipe))

    def test_token_burst(self):
        recipe = self.meter(burst_sessions=0, burst_tokens_per_minute=50000)
        (path,) = self.sessions(1)
        recipe.poll()
        self.append(path, assistant("msg_1", inp=0, out=10000, cc=45000))
        self.assertEqual(["vibe-1", "vibe-alert"], self.tick(recipe))

    def test_messages_never_hold_content(self):
        recipe = self.meter()
        (path,) = self.sessions(1)
        recipe.poll()
        self.append(path, assistant("msg_x"), user())
        self.tick(recipe)
        self.assertFalse(any(SECRET in m for m in self.messages))

    def test_projects_dir_is_not_agent_settable(self):
        with self.assertRaises(SettingsError):
            SCHEMA.check_agent_changes({"projects_dir": "C:/Windows"})
        self.assertEqual({"burst_sessions": 5}, SCHEMA.check_agent_changes({"burst_sessions": 5}))


if __name__ == "__main__":
    unittest.main()
