"""Tests for Audience.evaluate_health, driven by a FakePlatform."""

import time

from audiencelib.core import Audience
from tests.fake_platform import FakePlatform


def _app(**platform_overrides):
    plat = FakePlatform(**platform_overrides)
    return Audience(plat, url="http://test", interval=60), plat


def _keys(findings):
    return {key: tier for key, tier, _fact in findings}


def test_no_findings_when_healthy():
    app, _ = _app()
    assert app.evaluate_health() == []


def test_low_battery_tiers():
    app, _ = _app(battery={"percent": 8, "state": "discharging"})
    assert _keys(app.evaluate_health())["battery_low"] == 2   # <=10
    app, _ = _app(battery={"percent": 4, "state": "discharging"})
    assert _keys(app.evaluate_health())["battery_low"] == 3   # <=5
    app, _ = _app(battery={"percent": 18, "state": "discharging"})
    assert _keys(app.evaluate_health())["battery_low"] == 1   # <=20


def test_charging_battery_no_finding():
    app, _ = _app(battery={"percent": 8, "state": "charging"})
    assert "battery_low" not in _keys(app.evaluate_health())


def test_battery_drain_rate():
    app, _ = _app(battery={"percent": 20, "state": "discharging"})
    # seed a prior sample 6 minutes ago at 30% -> ~100%/hr drain (well over the
    # 50%/hr tier-2 threshold, with margin for elapsed-time slop)
    app._last_batt = (30, time.monotonic() - 360)
    keys = _keys(app.evaluate_health())
    assert keys.get("battery_drain") == 2


def test_cpu_high_ratio():
    app, _ = _app(loadavg=(8.0, 4.0, 2.0), cpus=4)   # ratio 2.0
    assert _keys(app.evaluate_health())["cpu_high"] == 2
    app, _ = _app(loadavg=(5.0, 4.0, 2.0), cpus=4)   # ratio 1.25
    assert _keys(app.evaluate_health())["cpu_high"] == 1


def test_low_memory_tiers():
    app, _ = _app(free_mem_mb=150)
    assert _keys(app.evaluate_health())["mem_low"] == 2
    app, _ = _app(free_mem_mb=400)
    assert _keys(app.evaluate_health())["mem_low"] == 1


def test_failed_probes_are_omitted():
    app, _ = _app(battery=None, loadavg=None, free_mem_mb=None)
    assert app.evaluate_health() == []
