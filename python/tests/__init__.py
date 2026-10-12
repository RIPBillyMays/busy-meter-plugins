"""Test package. Guard: no test may open the real HKCU Run key. WindowsRegistry only accepts a throwaway
``Software\\BusyMeterTest-<random>`` key here; anything else (the default Run key) fails the test that tried."""

from busymeter.recipes import autostart as _autostart

TEST_KEY_PREFIX = "Software\\BusyMeterTest-"
_real_init = _autostart.WindowsRegistry.__init__


def _guarded_init(self, path=_autostart.RUN_KEY):
    if not str(path).startswith(TEST_KEY_PREFIX):
        raise AssertionError(f"a test opened the registry key {path!r}; use FakeRegistry")
    _real_init(self, path)


_autostart.WindowsRegistry.__init__ = _guarded_init
