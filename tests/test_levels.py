import math
from array import array

import pytest

from omarvis.levels import LevelThrottle, rms_level


def pcm(samples):
    values = array("h", samples)
    return values.tobytes()


def test_rms_level_silence():
    assert rms_level(pcm([0] * 100)) == 0.0


def test_rms_level_full_scale_square_wave():
    assert rms_level(pcm([32767, -32768] * 100)) == pytest.approx(1.0, abs=0.001)


def test_rms_level_half_scale_sine():
    samples = [int(16384 * math.sin(2 * math.pi * index / 100)) for index in range(400)]
    assert rms_level(pcm(samples)) == pytest.approx(0.354, abs=0.002)


def test_level_throttle_limits_regular_updates_and_keeps_silence_edge():
    now = [0.0]
    emitted = []
    levels = LevelThrottle(lambda in_level, out_level: emitted.append((in_level, out_level)), clock=lambda: now[0])

    levels.update_in(0.5)
    now[0] = 0.05
    levels.update_in(0.7)
    now[0] = 0.06
    levels.update_in(0.0)
    now[0] = 0.09
    levels.update_out(0.4)
    now[0] = 0.16
    levels.update_out(0.5)

    assert emitted == [(0.5, 0.0), (0.0, 0.0), (0.0, 0.5)]


def test_level_throttle_clamps_and_rounds_values():
    emitted = []
    levels = LevelThrottle(lambda in_level, out_level: emitted.append((in_level, out_level)))

    levels.update_in(1.5)
    levels.update_out(-0.2)

    assert emitted[0] == (1.0, 0.0)


def test_level_throttle_force_emits_final_zero_once():
    emitted = []
    levels = LevelThrottle(lambda in_level, out_level: emitted.append((in_level, out_level)))
    levels.update_in(0.4)

    levels.force_zero()
    levels.force_zero()

    assert emitted[-1] == (0.0, 0.0)
    assert emitted.count((0.0, 0.0)) == 1
