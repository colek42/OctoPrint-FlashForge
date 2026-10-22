# coding=utf-8
"""Layer (b): off-chip devices on the board's buses, modelled from their datasheets."""


class Ads1118(object):
	"""TI ADS1118 16-bit ADC with internal temperature sensor (datasheet SBAS457, section 8.5).

	SPI mode 1. Each frame clocks the 16-bit config word in on DIN while DOUT shifts out the
	result of the previous conversion, optionally followed by the config readback. A new config
	takes effect when the frame ends (CS high, or after 16 bits if CS is not toggled).

	Conversion results are available immediately (conversion time is not modelled: at the
	PR #6553 data rate the real chip needs ~8 ms, so a firmware that reads faster than that would
	see stale data on hardware but fresh data here).

	`channels` maps the MUX field (bits 14:12) to a callable returning the differential input
	in microvolts; `die_temp` returns the internal sensor temperature in C.
	"""

	FSR = {0: 6.144, 1: 4.096, 2: 2.048, 3: 1.024, 4: 0.512, 5: 0.256, 6: 0.256, 7: 0.256}

	def __init__(self, cs_level, channels, die_temp):
		self.cs_level = cs_level  # callable -> 0 when selected
		self.channels = channels
		self.die_temp = die_temp
		self.config = 0x058B  # power-on default (datasheet table 7)
		self.data = 0
		self._rx_bits = 0
		self._rx = 0
		self._tx = []
		self.frames = 0
		self.last_mux = None

	def selected(self):
		return self.cs_level() == 0

	def _latch(self, word):
		if (word >> 1) & 3 != 1:  # NOP field must be 01 for the data to be written (table 8)
			return
		self.config = word
		self.frames += 1
		self.data = self._convert()

	def _convert(self):
		cfg = self.config
		if cfg & 0x10:  # TS_MODE: 14-bit left-justified, 0.03125 C/LSB
			code = int(round(self.die_temp() / 0.03125))
			return (code << 2) & 0xFFFF
		mux = (cfg >> 12) & 7
		self.last_mux = mux
		pga = (cfg >> 9) & 7
		lsb_uv = self.FSR[pga] / 32768.0 * 1e6
		src = self.channels.get(mux)
		uv = src() if src else 0.0
		code = int(round(uv / lsb_uv))
		code = max(-32768, min(32767, code))
		return code & 0xFFFF

	def transfer(self, word, bits):
		if not self._tx:
			self._tx = [self.data >> 8 & 0xFF, self.data & 0xFF, self.config >> 8 & 0xFF, self.config & 0xFF]
			if bits == 16:
				self._tx = [self.data, self.config]
		out = self._tx.pop(0) if self._tx else 0
		self._rx = (self._rx << bits) | (word & ((1 << bits) - 1))
		self._rx_bits += bits
		return out

	def cs_changed(self, level):
		"""Frame boundary. The first 16 bits clocked in are the config word (datasheet 8.5.4)."""
		if level == 1:
			if self._rx_bits >= 16:
				self._latch((self._rx >> (self._rx_bits - 16)) & 0xFFFF)
			self._rx_bits = 0
			self._rx = 0
			self._tx = []
