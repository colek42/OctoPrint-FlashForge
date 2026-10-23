# coding=utf-8
"""Layer (b): off-chip devices on the board's buses, modelled from their datasheets."""


class I2cEeprom(object):
	"""24Cxx I2C EEPROM on two GPIO lines (open drain, pulled up), 1- or 2-byte word address.

	Bit-level slave per the I2C spec (NXP UM10204): START/STOP while SCL is high, data sampled
	on SCL rising, the slave changes SDA only while SCL is low, ACK in the 9th clock. Sequential
	reads and writes; the 8-byte page wrap and the 5 ms write cycle are not modelled.
	"""

	def __init__(self, sda, scl, address=0x50, content=b"", size=256, addr_bytes=1):
		self.sda_g, self.sda_p = sda
		self.scl_g, self.scl_p = scl
		self.address = address
		self.mem = bytearray(size)
		self.addr_bytes = addr_bytes
		self.mem[:len(content[:size])] = content[:size]
		self.drive_low = False
		self.state = "idle"
		self.purpose = "addr"
		self.bits = self.byte = self.ptr = self.out = 0
		self.reading = False
		self.master_acked = False
		self.writes = 0
		self._sda_prev = 1
		self.sda_g.inputs[self.sda_p] = self.line
		self.sda_g.listen(self.sda_p, self._sda_edge)
		self.scl_g.listen(self.scl_p, self._scl_edge)

	def line(self):
		"""Wired-AND of the master (when its pin is an output) and this device."""
		master_out = (self.sda_g.regs[0x00] >> (2 * self.sda_p)) & 3 == 1
		if master_out and not (self.sda_g.odr() >> self.sda_p) & 1:
			return 0
		return 0 if self.drive_low else 1

	def _scl(self):
		return (self.scl_g.odr() >> self.scl_p) & 1

	def _sda_edge(self, level):
		line = self.line()
		if self._scl() and line != self._sda_prev:
			if line == 0:  # START or repeated START
				self.state, self.purpose, self.bits, self.byte, self.drive_low = "rx", "addr", 0, 0, False
			else:  # STOP
				self.state, self.drive_low = "idle", False
		self._sda_prev = line

	def _scl_edge(self, level):
		self._sda_prev = self.line()
		st = self.state
		if st == "idle":
			return
		if level:  # rising edge: the receiver samples
			if st == "rx":
				self.byte = ((self.byte << 1) | self.line()) & 0xFF
				self.bits += 1
			elif st == "tx":
				self.bits += 1
			elif st == "mack":
				self.master_acked = self.line() == 0
			return
		# falling edge: the transmitter may change SDA
		if st == "rx" and self.bits == 8:
			self._got_byte()
		elif st == "ack":
			self.drive_low = False
			if self.reading:
				self._start_tx()
			else:
				self.state, self.bits, self.byte = "rx", 0, 0
		elif st == "tx":
			if self.bits == 8:
				self.drive_low = False  # release SDA for the master's ACK/NACK
				self.ptr = (self.ptr + 1) % len(self.mem)
				self.state = "mack"
			else:
				self.drive_low = not (self.out >> (7 - self.bits)) & 1
		elif st == "mack":
			if self.master_acked:
				self._start_tx()
			else:
				self.state = "idle"

	def _start_tx(self):
		self.state, self.bits, self.out = "tx", 0, self.mem[self.ptr]
		self.drive_low = not (self.out >> 7) & 1

	def _got_byte(self):
		b = self.byte
		if self.purpose == "addr":
			if b >> 1 != self.address:
				self.state = "idle"
				return
			self.reading = bool(b & 1)
			self.purpose, self._addr_left, self._addr = "word", self.addr_bytes, 0
		elif self.purpose == "word":
			self._addr = (self._addr << 8) | b
			self._addr_left -= 1
			if self._addr_left == 0:
				self.ptr = self._addr % len(self.mem)
				self.purpose = "data"
		else:
			self.mem[self.ptr] = b
			self.ptr = (self.ptr + 1) % len(self.mem)
			self.writes += 1
		self.state = "ack"
		self.drive_low = True


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
