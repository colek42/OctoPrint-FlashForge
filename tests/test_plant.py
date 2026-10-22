# coding=utf-8
"""Physics plant and device models on their own: no firmware, runs in seconds."""

import json

from ffsim.firmware.devices import Ads1118
from ffsim.firmware.plant import Plant, PlantParams, ntc_resistance, type_k_microvolts


def test_type_k_matches_nist_table():
	# NIST ITS-90 type K: 100 C -> 4.096 mV, 200 C -> 8.138 mV, 0 C -> 0
	assert abs(type_k_microvolts(0.0)) < 1
	assert abs(type_k_microvolts(100.0) - 4096) < 2
	assert abs(type_k_microvolts(200.0) - 8138) < 2


def test_epcos_ntc_hits_its_calibration_points():
	assert abs(ntc_resistance(25.0) - 100000) / 100000 < 1e-3
	assert abs(ntc_resistance(150.0) - 1641.9) / 1641.9 < 1e-3


def test_ads1118_returns_previous_conversion_and_temp_sensor():
	cs = [1]
	uv = {"v": 4096.0}
	ads = Ads1118(lambda: cs[0], {0: lambda: uv["v"]}, lambda: 30.0)

	def frame(config):
		cs[0] = 0
		hi = ads.transfer(config >> 8, 8)
		lo = ads.transfer(config & 0xFF, 8)
		cs[0] = 1
		ads.cs_changed(1)
		return hi << 8 | lo

	frame(0x0C62)  # AIN0-AIN1, +-0.256 V
	code = frame(0x0072)  # returns the AIN0 result, starts a temperature conversion
	assert abs(code - round(4096.0 / (0.256 / 32768 * 1e6))) <= 1
	temp = frame(0x0C62)
	assert (temp >> 2) * 0.03125 == 30.0


def test_hotend_reaches_210_in_about_90_seconds_at_full_power():
	plant = Plant()
	h = plant.heaters["T0"]
	h.duty = lambda t0, t1: 1.0
	t = 0.0
	while h.block < 210 and t < 300:
		t += 0.5
		plant.advance(t)
	assert 80 <= t <= 100, t
	assert h.sensor < h.block  # the sensor lags the block, as in MPC


def _drive(plant, name, speed_mm_s, distance_mm, accel=3000.0):
	"""Feed a trapezoidal step train straight into one axis, as a firmware's ISR would."""
	ax = plant.axes[name]
	ax.enabled = True
	ax.asleep = False
	t, v, x, steps = plant.t, 0.0, 0.0, 0
	dt = 1e-5
	target = distance_mm * ax.steps_per_mm
	while steps < target:
		remaining = (target - steps) / ax.steps_per_mm
		if v * v / (2 * accel) >= remaining:
			v = max(5.0, v - accel * dt)
		else:
			v = min(speed_mm_s, v + accel * dt)
		x += v * dt
		t += dt
		while x * ax.steps_per_mm > steps + 1 and steps < target:
			steps += 1
			ax.step_edge(t, -1)  # toward the middle of travel: x0 starts at 200, the frame ends at 238
	plant.advance(t + 0.2)
	return ax


def test_gentle_x_move_loses_nothing():
	ax = _drive(Plant(), "x0", 50.0, 120.0)
	assert ax.lost_steps == 0
	assert ax.following_error_max < 0.5


def test_x_motor_stalls_between_300_and_600mm_s():
	"""With the GUESS motor (0.40 N*m, 1.5 A, 2.8 mH, 24 V) a perfectly smooth 300 mm/s step
	train still holds and 600 mm/s stalls (the sweep put the edge between 300 and 450 mm/s). The
	stock firmware caps X at ~286 mm/s whatever F says, so through it nothing skips: see
	test_300mm_s_move_skips in tests/test_firmware.py, which fails for that reason."""
	assert _drive(Plant(), "x0", 300.0, 200.0, accel=20000.0).lost_steps == 0
	assert _drive(Plant(), "x0", 600.0, 200.0, accel=20000.0).lost_steps != 0


def test_params_load_overrides_and_rejects_typos(tmp_path):
	path = tmp_path / "p.json"
	path.write_text(json.dumps({"heaters": {"T0": {"block_heat_capacity": 22.3}}, "axes": {"x0": {"moving_mass": 0.61}}}))
	p = PlantParams.load(str(path))
	assert p.heaters["T0"].block_heat_capacity == 22.3 and p.axes["x0"].moving_mass == 0.61
	path.write_text(json.dumps({"heaters": {"T0": {"block_heat_capacty": 1}}}))
	try:
		PlantParams.load(str(path))
	except KeyError:
		pass
	else:
		raise AssertionError("typo accepted")


def test_accelerometer_csv_is_klipper_format(tmp_path):
	plant = Plant()
	plant.start_accel_capture("x0", "y")
	_drive(plant, "x0", 100.0, 20.0)
	out = tmp_path / "raw_data_x.csv"
	rows = plant.stop_accel_capture(str(out))
	assert rows
	assert out.read_text().splitlines()[0] == "#time,accel_x,accel_y,accel_z"
