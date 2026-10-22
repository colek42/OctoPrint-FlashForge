# coding=utf-8
"""Layer (c): the physical printer the firmware acts on. It sees pins and PWM duty, nothing else.

Thermal: the hotend model uses the structure and parameter names of Marlin/Kalico MPC
(Kalico klippy/extras/control_mpc.py, ControlMPC.temperature_update), so an MPC_CALIBRATE run
on the real printer yields numbers that drop straight into PlantParams:

    C dTb/dt = P*duty - (Tb - Ta)*h(fan) - (Tb - Ta)*v_e*A_f*rho_f*c_f
    dTs/dt   = (Tb - Ts) * sensor_responsiveness

Tb is the block, Ts the sensor (the sensor lags the block, as in MPC), v_e the filament feed
speed, A_f its cross section. The bed uses the same form with its own parameters.

Motion: each axis is a stepper rotor coupled to a moving mass through a belt (or a lead screw for Z):

    J  th''  = T_motor(w, load angle) - r*F
    m  x''   = F - friction - m*g (Z only)
    F        = k*(r*th - x) + c*(r*th' - x')
    T_motor  = T_avail(w) * sin(Np * (th_cmd - th))      (Np = full steps per rev / 4)

T_avail falls with speed as the chopper runs out of voltage against back-EMF and winding
inductance. When the load angle passes +-pi the rotor slips to the next stable equilibrium
(4 full steps away); every slip is counted as lost steps and the true carriage position
drifts from what the firmware's step count implies. This is the point of the model: it says
which feedrates and accelerations would skip on the real machine.

Every number lives in PlantParams with its source. Anything marked GUESS is an estimate with the
reasoning next to it; replace it with a measurement through PlantParams.load(path).
"""

import dataclasses
import json
import math
import threading

AMBIENT = 25.0


@dataclasses.dataclass
class HeaterParams:
	heater_power: float  # W at full duty
	block_heat_capacity: float  # J/K
	ambient_transfer: float  # W/K, fan off
	fan_ambient_transfer: tuple  # W/K at fan 0..1, evenly spaced (Kalico convention)
	sensor_responsiveness: float  # 1/s
	max_temp: float = 300.0  # a runaway clamp for the plant only, not a firmware limit
	source: str = ""


@dataclasses.dataclass
class MotorParams:
	holding_torque: float = 0.40  # N*m. GUESS: typical 40 N*cm NEMA17 (e.g. 17HS4401); motor model unknown
	rated_current: float = 1.5  # A. GUESS: same datasheet class; the board sets current via a digipot
	phase_resistance: float = 1.5  # ohm. GUESS: 17HS4401 datasheet class
	phase_inductance: float = 2.8e-3  # H. GUESS: 17HS4401 datasheet class
	rotor_inertia: float = 54e-7  # kg*m^2. GUESS: 54 g*cm^2, 17HS4401 class
	full_steps: int = 200  # 1.8 deg motor: Klipper PR #6553 uses microsteps 16 and rotation_distance per 200 steps
	supply_voltage: float = 24.0  # V. GUESS: FFCP2 PSU rating not verified
	damping_ratio: float = 0.1  # of the rotor-on-holding-torque mode. GUESS: chopper drive, no damper
	source: str = "GUESS: generic 1.8 deg NEMA17, see field comments"


@dataclasses.dataclass
class AxisParams:
	name: str
	moving_mass: float  # kg
	rotation_distance: float  # mm of travel per motor revolution
	microsteps: int
	stiffness_ea: float = 1.0e5  # N; belt k = EA / free length. GUESS: GT2 6 mm order of magnitude
	free_length: float = 0.4  # m of free belt between pulley and carriage. GUESS: half of a ~0.8 m loop
	damping_ratio: float = 0.05  # GUESS: lightly damped belt drive
	friction: float = 2.0  # N Coulomb friction at the carriage. GUESS
	gravity: bool = False  # True for the Z bed
	lead_screw: bool = False  # Z: rigid coupling through a screw
	screw_efficiency: float = 0.35  # GUESS: trapezoidal lead screw
	position_min: float = 0.0  # mm, machine coordinates (Klipper frame of PR #6553)
	position_max: float = 200.0
	endstop: str = "max"  # which end the switch sits at
	endstop_position: float = 200.0
	hard_stop_margin: float = 2.0  # mm past the endstop before the frame stops the carriage. GUESS
	start: float = 100.0  # where the carriage is at power-on
	source: str = ""


def _default_axes():
	k = "Klipper PR #6553 config/printer-flashforge-creator-pro2.cfg"
	return {
		# Right carriage: stepper_x (step !PB5, dir PB6, endstop PC6, homes to max 236)
		"x0": AxisParams("x0", 0.52, 34.0, 16, position_min=-38.0, position_max=236.0, endstop="max",
						 endstop_position=236.0, start=200.0,
						 source="mass GUESS 0.45-0.6 kg (motor ~0.28 kg + hotend + fans + carriage); "
								"travel/steps: " + k),
		# Left carriage: dual_carriage (step !PC2, dir !PC3, endstop PC7, homes to min -38)
		"x1": AxisParams("x1", 0.52, 34.0, 16, position_min=-38.0, position_max=198.0, endstop="min",
						 endstop_position=-38.0, start=0.0, source="as x0; " + k),
		# Y moves the X gantry with both carriages (see docs: how this was inferred)
		"y": AxisParams("y", 1.75, 34.0, 16, position_min=-3.0, position_max=152.0, endstop="min",
						endstop_position=-3.0, free_length=0.3, start=75.0,
						source="mass GUESS 1.5-2 kg (rail + two carriages); travel: " + k),
		# Z bed on a lead screw (rotation_distance 8)
		"z": AxisParams("z", 1.2, 8.0, 16, stiffness_ea=0.0, gravity=True, lead_screw=True,
						position_min=-4.0, position_max=172.0, endstop="min", endstop_position=-4.0,
						damping_ratio=0.3, friction=5.0, start=50.0,
						source="mass GUESS 1.2 kg (aluminium plate + glass + carriage); travel: " + k),
		# Extruders: direct drive, rotation_distance 32 (PR #6553). Mass = filament + gear, tiny.
		"e0": AxisParams("e0", 0.01, 32.0, 16, stiffness_ea=0.0, lead_screw=True, friction=0.0,
						 position_min=-1e9, position_max=1e9, endstop="none", endstop_position=0.0, start=0.0,
						 source="right extruder (step PE0, dir PE1): " + k),
		"e1": AxisParams("e1", 0.01, 32.0, 16, stiffness_ea=0.0, lead_screw=True, friction=0.0,
						 position_min=-1e9, position_max=1e9, endstop="none", endstop_position=0.0, start=0.0,
						 source="left extruder (step PE5, dir !PE6): " + k),
	}


def _default_heaters():
	return {
		# Hotend: 40 W, ~90 s from 25 to 210 C (task brief). With h = 0.10 W/K the steady state
		# at full power is 25 + 40/0.10 = 425 C; C = 14.5 J/K gives tau = 145 s and 25 -> 210 C
		# in 145*ln(400/215) = 90 s. Sensor responsiveness from Kalico docs/MPC.md example.
		"T0": HeaterParams(40.0, 14.5, 0.10, (0.10, 0.13, 0.15), 0.0999,
						   source="GUESS from brief (40 W, 210 C in ~90 s); responsiveness: Kalico MPC.md example"),
		"T1": HeaterParams(40.0, 14.5, 0.10, (0.10, 0.13, 0.15), 0.0999, source="as T0"),
		# Bed: ~60 C in ~3 min (brief). 150 W, C = 600 J/K (0.65 kg aluminium * 900 J/kg/K),
		# h = 1.5 W/K -> tau 400 s, 25 -> 61 C in 180 s.
		"B": HeaterParams(150.0, 600.0, 1.5, (1.5,), 0.2, max_temp=150.0,
						  source="GUESS from brief (60 C in ~3 min); mass from plate size estimate"),
	}


@dataclasses.dataclass
class PlantParams:
	ambient: float = AMBIENT
	heaters: dict = dataclasses.field(default_factory=_default_heaters)
	axes: dict = dataclasses.field(default_factory=_default_axes)
	motor: MotorParams = dataclasses.field(default_factory=MotorParams)
	# ADS1118 internal sensor reads the board temperature: used as the thermocouple cold junction
	board_temp_rise: float = 5.0  # C above ambient. GUESS
	# Bed NTC: EPCOS 100K B57560G104F (PR #6553 sensor_type) on PC4 through a pull-up divider
	ntc_pullup: float = 4700.0  # ohm. GUESS: Klipper's default pullup_resistor; verify from firmware readout
	adc_vref: float = 3.3
	filament_diameter: float = 1.75  # mm
	filament_density: float = 1.20  # g/cm^3, Kalico default
	filament_heat_capacity: float = 1.80  # J/g/K, Kalico default
	dt_motion: float = 20e-6  # s, integration step (stiffest mode ~300 Hz)
	accel_rate: float = 3200.0  # Hz, ADXL345 capture rate used by Klipper

	@classmethod
	def load(cls, path):
		"""Override defaults from JSON (or YAML if PyYAML is installed). Unknown keys are errors."""
		with open(path) as f:
			text = f.read()
		try:
			data = json.loads(text)
		except ValueError:
			import yaml
			data = yaml.safe_load(text)
		p = cls()
		for key, value in data.items():
			if key == "heaters":
				for name, fields in value.items():
					for k, v in fields.items():
						if not hasattr(p.heaters[name], k):
							raise KeyError("heaters.%s.%s" % (name, k))
						setattr(p.heaters[name], k, tuple(v) if isinstance(v, list) else v)
			elif key == "axes":
				for name, fields in value.items():
					for k, v in fields.items():
						if not hasattr(p.axes[name], k):
							raise KeyError("axes.%s.%s" % (name, k))
						setattr(p.axes[name], k, v)
			elif key == "motor":
				for k, v in value.items():
					if not hasattr(p.motor, k):
						raise KeyError("motor.%s" % k)
					setattr(p.motor, k, v)
			elif hasattr(p, key):
				setattr(p, key, value)
			else:
				raise KeyError(key)
		return p

	def dump(self):
		return dataclasses.asdict(self)


# ---- sensors --------------------------------------------------------------------------------

# Type K thermocouple EMF in microvolts, NIST ITS-90 (srdata.nist.gov/its90/download/type_k.tab).
# Coefficients copied from Klipper PR #6553 klippy/extras/ads1118.py,
# Copyright (C) 2023 Christoph Nelles <klipper-fw@evilazrael.de>, GPLv3.
_K_SUB_ZERO = [0.0, 0.394501280250e-1, 0.236223735980e-4, -0.328589067840e-6, -0.499048287770e-8,
			   -0.675090591730e-10, -0.574103274280e-12, -0.310888728940e-14, -0.104516093650e-16,
			   -0.198892668780e-19, -0.163226974860e-22]
_K_ABOVE_ZERO = [-0.176004136860e-1, 0.389212049750e-1, 0.185587700320e-4, -0.994575928740e-7,
				 0.318409457190e-9, -0.560728448890e-12, 0.560750590590e-15, -0.320207200030e-18,
				 0.971511471520e-22, -0.121047212750e-25]
_K_EXP = [0.1185976, -0.118343200000e-3, 0.126968600000e3]


def type_k_microvolts(temp_c):
	"""Thermocouple EMF (uV) for a junction at temp_c against a 0 C reference."""
	if temp_c <= 0.0:
		mv = sum(c * temp_c ** i for i, c in enumerate(_K_SUB_ZERO))
	else:
		mv = _K_EXP[0] * math.exp(_K_EXP[1] * (temp_c - _K_EXP[2]) ** 2)
		mv += sum(c * temp_c ** i for i, c in enumerate(_K_ABOVE_ZERO))
	return mv * 1000.0


# EPCOS 100K B57560G104F, Steinhart-Hart through Klipper's three calibration points
# (klippy/extras/thermistor.py: t1=25 r1=100000, t2=150 r2=1641.9, t3=250 r3=226.15).
def _steinhart(points):
	(t1, r1), (t2, r2), (t3, r3) = [(t + 273.15, r) for t, r in points]
	l1, l2, l3 = math.log(r1), math.log(r2), math.log(r3)
	y1, y2, y3 = 1 / t1, 1 / t2, 1 / t3
	g2, g3 = (y2 - y1) / (l2 - l1), (y3 - y1) / (l3 - l1)
	c = (g3 - g2) / (l3 - l2) / (l1 + l2 + l3)
	b = g2 - c * (l1 * l1 + l1 * l2 + l2 * l2)
	a = y1 - (b + l1 * l1 * c) * l1
	return a, b, c


_EPCOS = _steinhart([(25.0, 100000.0), (150.0, 1641.9), (250.0, 226.15)])


def ntc_resistance(temp_c, coeffs=_EPCOS):
	a, b, c = coeffs
	y = 1.0 / (temp_c + 273.15)
	# invert 1/T = a + b ln R + c ln^3 R for ln R (Cardano)
	x = (a - y) / c
	q = b / c
	d = math.sqrt((q / 3.0) ** 3 + (x / 2.0) ** 2)
	ln_r = (d - x / 2.0) ** (1.0 / 3.0) - (d + x / 2.0) ** (1.0 / 3.0)
	return math.exp(ln_r)


# ---- thermal --------------------------------------------------------------------------------


class Heater(object):
	def __init__(self, params, plant, name):
		self.p, self.plant, self.name = params, plant, name
		self.block = self.sensor = plant.params.ambient
		self.duty = lambda t0, t1: 0.0  # board wires this to a PWM channel or a GPIO time-average
		self.fan = lambda: 0.0
		self.feed_speed = lambda: 0.0  # filament mm/s into this hotend
		self.energy = 0.0

	def step(self, t0, t1):
		dt = t1 - t0
		if dt <= 0:
			return
		p = self.p
		duty = max(0.0, min(1.0, self.duty(t0, t1)))
		h = _interp(p.fan_ambient_transfer, self.fan()) if p.fan_ambient_transfer else p.ambient_transfer
		if self.fan() <= 0.0:
			h = p.ambient_transfer
		ta = self.plant.params.ambient
		pp = self.plant.params
		area = math.pi * (pp.filament_diameter / 2.0) ** 2  # mm^2
		fxs = area * 1e-3 * pp.filament_density * pp.filament_heat_capacity  # J/K per mm of filament
		power = p.heater_power * duty
		self.energy += power * dt
		# sub-step so the explicit update stays stable for large plant ticks
		n = max(1, int(dt / 0.01))
		h_dt = dt / n
		for _ in range(n):
			loss = (self.block - ta) * (h + self.feed_speed() * fxs)
			self.block += (power - loss) * h_dt / p.block_heat_capacity
			self.block = min(self.block, p.max_temp)
			self.sensor += (self.block - self.sensor) * min(1.0, p.sensor_responsiveness * h_dt)


def _interp(table, x):
	x = max(0.0, min(1.0, x))
	if len(table) == 1:
		return table[0]
	pos = x * (len(table) - 1)
	i = int(pos)
	if i >= len(table) - 1:
		return table[-1]
	f = pos - i
	return table[i] * (1 - f) + table[i + 1] * f


# ---- motion ---------------------------------------------------------------------------------


class Axis(object):
	"""One stepper driving one moving mass. Positions in mm, machine frame."""

	def __init__(self, params, motor, plant):
		self.p, self.motor, self.plant = params, motor, plant
		self.steps_per_mm = motor.full_steps * params.microsteps / params.rotation_distance
		self.r = params.rotation_distance / (2 * math.pi) * 1e-3  # m of travel per rad
		self.np = motor.full_steps / 4.0  # electrical cycles per revolution
		self.steps = 0  # driver step count since power-on
		self.enabled = False
		self.theta = 0.0  # rotor angle, rad, 0 at power-on
		self.omega = 0.0
		self.x = 0.0  # carriage displacement from power-on position, m
		self.v = 0.0
		self.a = 0.0
		self.t = 0.0
		self.lost_full_steps = 0
		self.slip_index = 0
		self.following_error_max = 0.0  # mm
		self.asleep = True
		self._quiet = 0.0
		if params.lead_screw:
			# rigid screw/gear: lump mass into the rotor via the transmission ratio
			self.k = 0.0
			self.c = 0.0
		else:
			self.k = params.stiffness_ea / params.free_length
			m_eff = params.moving_mass
			self.c = 2.0 * params.damping_ratio * math.sqrt(self.k * m_eff)
		j = motor.rotor_inertia + (params.moving_mass * self.r ** 2 if params.lead_screw else 0.0)
		k_el = motor.holding_torque * self.np  # N*m/rad around the stable equilibrium
		self.c_rotor = 2.0 * motor.damping_ratio * math.sqrt(k_el * j)
		self.hard_min = params.position_min - params.hard_stop_margin
		self.hard_max = params.position_max + params.hard_stop_margin

	# true carriage position in machine mm
	@property
	def position(self):
		if self.p.lead_screw:
			return self.p.start + self.r * self.theta * 1e3
		return self.p.start + self.x * 1e3

	@property
	def commanded(self):
		return self.p.start + self.steps / self.steps_per_mm

	@property
	def lost_steps(self):
		"""Lost microsteps (signed sum of slips), in the same unit the firmware counts."""
		return self.lost_full_steps * self.p.microsteps

	def _theta_cmd(self):
		return self.steps * 2 * math.pi / (self.motor.full_steps * self.p.microsteps)

	def torque_available(self, omega):
		m = self.motor
		we = abs(omega) * self.np  # electrical rad/s
		kt = m.holding_torque / m.rated_current
		v_eff = max(0.0, m.supply_voltage - kt * abs(omega))
		i = min(m.rated_current, v_eff / math.sqrt(m.phase_resistance ** 2 + (we * m.phase_inductance) ** 2))
		return kt * i

	def step_edge(self, t, direction):
		self.advance(t)
		if not self.enabled:
			return
		self.steps += direction
		self.asleep = False
		self._quiet = 0.0

	def advance(self, t):
		if t <= self.t:
			return
		if self.asleep:
			self.t = t
			return
		dt_max = self.plant.params.dt_motion
		start = self.t
		while self.t < t:
			dt = min(dt_max, t - self.t)
			self._integrate(dt)
			self.t += dt
			self.plant._sample_accel(self)
		if abs(self.omega) < 1e-3 and abs(self.v) < 1e-5 and abs(self.a) < 1e-1:
			self._quiet += t - start
			if self._quiet > 0.05:
				self.asleep = True  # state frozen until the next step edge or enable change
		else:
			self._quiet = 0.0

	def _integrate(self, dt):
		p, m = self.p, self.motor
		load = self.np * (self._theta_cmd() - self.theta)
		k_slip = int(math.floor((load + math.pi) / (2 * math.pi)))
		if k_slip != self.slip_index:
			self.lost_full_steps += 4 * (k_slip - self.slip_index)
			self.slip_index = k_slip
		torque = self.torque_available(self.omega) * math.sin(load) if self.enabled else 0.0
		# driver/rotor viscous damping, sized for damping_ratio of the holding-torque spring
		torque -= self.c_rotor * self.omega
		if p.lead_screw:
			# rotor + reflected mass on a screw or extruder gear; gravity enters as torque
			j = m.rotor_inertia + p.moving_mass * self.r ** 2
			if not self.enabled and p.screw_efficiency < 0.5:
				# a screw under 50% efficiency is self-locking: gravity cannot back-drive it
				self.omega = self.a = 0.0
				return
			load_force = (p.moving_mass * 9.81 if p.gravity else 0.0)
			t_load = load_force * self.r / p.screw_efficiency
			fric = p.friction * self.r * _sign(self.omega)
			self.a = (torque - t_load - fric) / j
			self.omega += self.a * dt
			self.theta += self.omega * dt
			self._clamp_screw()
		else:
			f = self.k * (self.r * self.theta - self.x) + self.c * (self.r * self.omega - self.v)
			alpha = (torque - self.r * f) / m.rotor_inertia
			fric = p.friction * _sign(self.v) if abs(self.v) > 1e-6 else max(-p.friction, min(p.friction, f))
			acc = (f - fric) / p.moving_mass
			self.omega += alpha * dt
			self.theta += self.omega * dt
			self.v += acc * dt
			self.x += self.v * dt
			self.a = acc
			self._clamp_belt()
		# dynamic lag only: steps already lost (slips, crashes into the frame) are not re-counted
		err = abs(self.commanded - self.lost_steps / self.steps_per_mm - self.position)
		if err > self.following_error_max:
			self.following_error_max = err

	def _clamp_belt(self):
		pos = self.position
		if pos < self.hard_min or pos > self.hard_max:
			lim = self.hard_min if pos < self.hard_min else self.hard_max
			self.x = (lim - self.p.start) * 1e-3
			self.v = 0.0

	def _clamp_screw(self):
		pos = self.position
		if pos < self.hard_min or pos > self.hard_max:
			lim = self.hard_min if pos < self.hard_min else self.hard_max
			self.theta = (lim - self.p.start) * 1e-3 / self.r
			self.omega = 0.0

	def endstop_triggered(self):
		p = self.p
		if p.endstop == "max":
			return self.position >= p.endstop_position
		if p.endstop == "min":
			return self.position <= p.endstop_position
		return False


def _sign(v):
	return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)


class Plant(object):
	def __init__(self, params=None):
		self.params = params or PlantParams()
		self.lock = threading.RLock()
		self.heaters = {k: Heater(v, self, k) for k, v in self.params.heaters.items()}
		self.axes = {k: Axis(v, self.params.motor, self) for k, v in self.params.axes.items()}
		self.t = 0.0
		self.accel_capture = None  # list of (t, ax, ay, az) in mm/s^2 while capturing
		self._accel_next = 0.0
		self._accel_axes = None

	def advance(self, t):
		"""Bring every model up to simulated time t (seconds)."""
		with self.lock:
			if t <= self.t:
				return
			for ax in self.axes.values():
				ax.advance(t)
			for h in self.heaters.values():
				h.step(self.t, t)
			self.t = t

	def board_temp(self):
		return self.params.ambient + self.params.board_temp_rise

	# ---- accelerometer (Klipper ADXL345 CSV: "#time,accel_x,accel_y,accel_z", mm/s^2) ----

	def start_accel_capture(self, x_axis="x0", y_axis="y"):
		with self.lock:
			self.accel_capture = []
			self._accel_axes = (x_axis, y_axis)
			self._accel_next = self.t

	def _sample_accel(self, axis):
		if self.accel_capture is None or axis.p.name != self._accel_axes[0]:
			return
		if axis.t < self._accel_next:
			return
		self._accel_next += 1.0 / self.params.accel_rate
		ay_axis = self.axes[self._accel_axes[1]]
		self.accel_capture.append((axis.t, axis.a * 1e3, ay_axis.a * 1e3, 9810.0))

	def stop_accel_capture(self, path=None):
		with self.lock:
			data, self.accel_capture = self.accel_capture or [], None
		if path:
			with open(path, "w") as f:
				f.write("#time,accel_x,accel_y,accel_z\n")
				for row in data:
					f.write("%.6f,%.3f,%.3f,%.3f\n" % row)
		return data
