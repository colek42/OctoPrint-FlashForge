# coding=utf-8
"""FlashForge Creator Pro 2 mainboard: which MCU pin goes to which physical thing.

Firmware-independent: this is copper, not code. Sources, cross-checked where both say something:
  [K]  Klipper PR #6553, config/printer-flashforge-creator-pro2.cfg (step/dir/enable inversion,
       endstop polarity, heater pins, ADS1118 cs PF12 on spi3a, rotation_distance, microsteps)
  [B]  BrennerLu/Klipper4CreatorPro2 reverseEngineering/PinOut-Text.txt (CubeMX labels)
  [fw] what the stock firmware actually configured, observed in emulation (MODER/AF/CCER)
Disagreements are listed in docs/ffsim-firmware-backend.md.
"""

from .devices import Ads1118, I2cEeprom
from .plant import ntc_resistance, type_k_microvolts

# axis: (step pin, step inverted, dir pin, dir inverted, enable pin, enable inverted)   [K], labels [B]
STEPPERS = {
	"x0": ("PB5", True, "PB6", False, "PB4", True),  # [K] stepper_x, right carriage; [B] DRV_XR1
	"x1": ("PC2", True, "PC3", True, "PC1", True),  # [K] dual_carriage, left carriage; [B] DRV_XL1
	"y": ("PF8", True, "PF9", True, "PF7", True),  # [K] stepper_y; [B] DRV_Y1
	"z": ("PF3", False, "PF4", True, "PF2", True),  # [K] stepper_z; [B] DRV_Z1
	"e0": ("PE0", False, "PE1", False, "PB9", True),  # [K] extruder (right); [B] DRV_XA
	"e1": ("PE5", False, "PE6", True, "PE4", True),  # [K] extruder1 (left); [B] DRV_XB
}
# the two "VS??" pins per driver [B]; see MicrostepPins below
VS_PINS = {"x0": ("PG15", "PB3"), "x1": ("PF10", "PC0"), "y": ("PF5", "PF6"), "z": ("PF0", "PF1"),
		   "e0": ("PB7", "PB8"), "e1": ("PE2", "PE3")}
# endstop pin, level when triggered   [K] (PC6/PC7 not inverted, PG0/PG1 inverted); labels [B]
ENDSTOPS = {"x0": ("PC6", 1), "x1": ("PC7", 1), "y": ("PG0", 0), "z": ("PG1", 0)}
# heater: (pin, timer channel when the pin is in alternate-function mode)  pins [K][B]; TIM5 [fw]+RM0090 AF2
HEATERS = {"T0": ("PA3", ("TIM5", 4)), "T1": ("PA2", ("TIM5", 3)), "B": ("PA1", ("TIM5", 2))}
# part-cooling fans feeding the hotend model's fan term [K][B]
PART_FANS = {"T0": "PF13", "T1": "PB0"}
ADS1118_CS = "PF12"  # [K] ads1118 cs_pin; SPI3 on PC10/PC11/PC12 [K spi3a][B][fw AF6]
ADS1118_MUX = {0: "T0", 3: "T1"}  # [K] ads1118_mux 0 -> [extruder] (right), 3 -> [extruder1] (left)
BED_NTC_ADC_CHANNEL = 14  # [B] ADC1_IN14 on PC4; [K] sensor_pin PC4


class PinTimeAverage(object):
	"""Fraction of time a GPIO output was high over an interval, from its edge history."""

	def __init__(self, machine, gpio, pin):
		self.m, self.gpio, self.pin = machine, gpio, pin
		self.level = gpio.level(pin)
		self.since = 0.0
		self.high = 0.0
		gpio.listen(pin, self._edge)

	def _edge(self, level):
		now = self.m.now
		if self.level:
			self.high += now - self.since
		self.level, self.since = level, now

	def duty(self, t0, t1):
		if self.level:
			self.high += t1 - self.since
		self.since = t1
		h, self.high = self.high, 0.0
		return h / (t1 - t0) if t1 > t0 else float(self.level)


EEPROM_SDA, EEPROM_SCL, EEPROM_ADDRESS = "PG3", "PG4", 0x50  # [fw] bit-banged 0xA0 (FUN_0801b306); [B] labels PG3/PG4 "EEPROM"


def default_eeprom():
	"""Settings EEPROM contents for a printer we have never read. GUESS: all zero, which the
	stock firmware reads as zero T1 nozzle offsets (EEPROM 0x00/0x04/0x08, floats) instead of
	the NaN an erased (0xFF) part gives, plus a machine name at 0x40 (0x22 bytes). Replace with
	a dump of the real chip (FFSIM_EEPROM=path) when one exists."""
	img = bytearray(256)
	name = b"FFCP2-SIM"
	img[0x40:0x40 + len(name)] = name
	return bytes(img)


class Board(object):
	def __init__(self, soc, plant, eeprom=None):
		self.soc, self.plant, self.m = soc, plant, soc.m
		if eeprom is None:
			import os
			path = os.environ.get("FFSIM_EEPROM")
			eeprom = open(path, "rb").read() if path else default_eeprom()
		# 2-byte word addresses: the stock firmware sends A0 00 <addr> (observed); size GUESS 24C32
		self.eeprom = I2cEeprom(soc.pin(EEPROM_SDA), soc.pin(EEPROM_SCL), EEPROM_ADDRESS, eeprom,
								size=4096, addr_bytes=2)
		soc.die_temp = plant.board_temp  # MCU die sits on the same board as the ADS1118
		self._wire_steppers()
		self._wire_endstops()
		self._wire_heaters()
		self._wire_sensors()
		self.m.schedule_in(0.001, self._tick)

	# ---- motion ------------------------------------------------------------------------

	def _wire_steppers(self):
		for name, (step, step_inv, d, d_inv, en, en_inv) in STEPPERS.items():
			axis = self.plant.axes[name]
			sg, sp = self.soc.pin(step)
			dg, dp = self.soc.pin(d)
			eg, ep = self.soc.pin(en)
			active = 0 if step_inv else 1

			def on_step(level, axis=axis, active=active, dg=dg, dp=dp, d_inv=d_inv):
				if level != active:
					return
				positive = dg.level(dp) ^ d_inv
				axis.step_edge(self.m.now, 1 if positive else -1)

			def on_enable(level, axis=axis, en_inv=en_inv):
				axis.advance(self.m.now)
				axis.enabled = bool(level ^ en_inv)
				axis.asleep = False

			sg.listen(sp, on_step)
			eg.listen(ep, on_enable)
			axis.enabled = bool(eg.level(ep) ^ en_inv) if (eg.regs[0] >> (2 * ep)) & 3 == 1 else False

	def microstep_pins(self):
		"""Levels of each driver's two unlabelled pins ([B] 'VS??'). See docs for the reading."""
		out = {}
		for name, pins in VS_PINS.items():
			out[name] = tuple(self.soc.pin(p)[0].level(self.soc.pin(p)[1]) for p in pins)
		return out

	def _wire_endstops(self):
		for name, (pin, trig) in ENDSTOPS.items():
			g, p = self.soc.pin(pin)
			axis = self.plant.axes[name]

			def read(axis=axis, trig=trig):
				axis.advance(self.m.now)
				return trig if axis.endstop_triggered() else 1 - trig

			g.inputs[p] = read

	# ---- thermal -----------------------------------------------------------------------

	def _wire_heaters(self):
		for name, (pin, (tim, ch)) in HEATERS.items():
			g, p = self.soc.pin(pin)
			avg = PinTimeAverage(self.m, g, p)
			timer = self.soc.tim[tim]

			def duty(t0, t1, g=g, p=p, avg=avg, timer=timer, ch=ch):
				mode = (g.regs[0] >> (2 * p)) & 3
				gpio_duty = avg.duty(t0, t1)
				if mode == 2:  # alternate function: the timer channel drives the pin
					return self._pwm_duty(timer, ch)
				if mode == 1:
					return gpio_duty
				return 0.0

			self.plant.heaters[name].duty = duty
		for name, pin in PART_FANS.items():
			g, p = self.soc.pin(pin)
			avg = PinTimeAverage(self.m, g, p)
			state = {"duty": 0.0}

			def fan(g=g, p=p, avg=avg, state=state):
				return state["duty"]

			self.plant.heaters[name].fan = fan
			self._fans = getattr(self, "_fans", []) + [(avg, state)]

	@staticmethod
	def _pwm_duty(timer, ch):
		"""Duty of TIMx channel ch (1-4) from CCMR (OCxM), CCER (CCxE, CCxP), CCR and ARR."""
		r = timer.r
		ccer = r.get(0x20, 0)
		if not (ccer >> (4 * (ch - 1))) & 1 or not r.get(0x00, 0) & 1:
			return 0.0
		ccmr = r.get(0x18 if ch <= 2 else 0x1C, 0)
		ocm = (ccmr >> (4 if ch in (1, 3) else 12)) & 7
		d = min(1.0, r.get(0x30 + 4 * ch, 0) / float(timer.arr_active + 1))
		if ocm == 7:
			d = 1.0 - d
		elif ocm == 4:
			d = 0.0
		elif ocm == 5:
			d = 1.0
		elif ocm != 6:
			return 0.0
		if (ccer >> (4 * (ch - 1) + 1)) & 1:
			d = 1.0 - d
		return d

	def _wire_sensors(self):
		plant, soc = self.plant, self.soc
		# ADS1118 on SPI3, CS PF12
		cg, cp = soc.pin(ADS1118_CS)

		def tc_uv(heater):
			def read():
				plant.advance(self.m.now)
				cj = plant.board_temp()
				return type_k_microvolts(plant.heaters[heater].sensor) - type_k_microvolts(cj)
			return read

		self.ads = Ads1118(lambda: cg.level(cp), {mux: tc_uv(h) for mux, h in ADS1118_MUX.items()},
						   plant.board_temp)
		cg.listen(cp, self.ads.cs_changed)
		soc.spi["SPI3"].devices.append(self.ads)
		# DOUT doubles as /DRDY: low while selected and a result is ready (always ready here)
		g11, p11 = soc.pin("PC11")
		g11.inputs[p11] = lambda: 0 if self.ads.selected() else 1

		# bed NTC: 3.3 V -- pull-up -- PC4 -- NTC -- GND, 12-bit ADC
		def ntc_code():
			plant.advance(self.m.now)
			r = ntc_resistance(plant.heaters["B"].sensor)
			v = plant.params.adc_vref * r / (r + plant.params.ntc_pullup)
			return int(round(v / plant.params.adc_vref * 4095))

		soc.adc.channels[BED_NTC_ADC_CHANNEL] = ntc_code

	# ---- periodic plant update ---------------------------------------------------------

	def _tick(self):
		now = self.m.now
		for avg, state in getattr(self, "_fans", []):
			state["duty"] = avg.duty(now - 0.001, now)
		self.plant.advance(now)
		self.m.schedule_in(0.001, self._tick)
