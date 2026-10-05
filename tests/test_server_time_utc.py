"""Сервер считает, что живёт по UTC: метки в базе — UTC, какой бы TZ ни был у
процесса (L5 аудита)."""

import datetime as dt
import os
import time

import pytest

import db
import timeutil


@pytest.fixture
def los_angeles():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "America/Los_Angeles"
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def test_now_iso_is_utc_whatever_the_process_tz(los_angeles):
    assert dt.datetime.now().utcoffset() is None  # naive local time really differs below
    local = dt.datetime.now().replace(microsecond=0)
    utc = _utc()
    assert abs((local - utc).total_seconds()) > 3600  # LA is 7-8 h behind
    stamp = db.now_iso()
    assert len(stamp) == len("2026-01-01T00:00:00") and "+" not in stamp
    assert abs((dt.datetime.fromisoformat(stamp) - utc).total_seconds()) < 5
    assert abs((timeutil.utc_now() - utc).total_seconds()) < 5
