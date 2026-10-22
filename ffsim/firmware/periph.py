# coding=utf-8
"""Layer (b): STM32F4 peripheral models at register level (RM0090 offsets).

Only the behaviour firmware actually depends on is modelled; every model says what it skips.
These classes know nothing about which firmware runs or what is wired to the pins: wiring is the
board's job (ffsim/firmware/board.py).
"""

from .cpu import Register


class Rcc(Register):
	"""Clock control: every oscillator/PLL reports ready as soon as it is switched on."""

	def __init__(self):
		Register.__init__(self, "RCC")
		self.regs[0x00] = 0x83  # HSION | HSIRDY

	def read(self, off, size):
		base = off & ~3
		v = self.regs.get(base, 0)
		if base == 0x00:  # CR: xxxRDY mirrors xxxON
			for on, rdy in ((0, 1), (16, 17), (24, 25), (26, 27)):
				if v >> on & 1:
					v |= 1 << rdy
		elif base == 0x08:  # CFGR: SWS = SW
			v = (v & ~0xC) | ((v & 3) << 2)
		elif base == 0x70:  # BDCR: LSERDY mirrors LSEON
			v |= (v & 1) << 1
		elif base == 0x74:  # CSR: LSIRDY mirrors LSION
			v |= (v & 1) << 1
		return v >> (8 * (off & 3)) & ((1 << (8 * size)) - 1)


class Pwr(Register):
	def read(self, off, size):
		v = Register.read(self, off, size)
		if off & ~3 == 0x04:
			v |= 1 << 14  # VOSRDY
		return v


class Gpio(object):
	"""One GPIO port. Outputs notify listeners on change; inputs come from per-pin sources."""

	def __init__(self, machine, name):
		self.m, self.name = machine, name
		self.regs = {0x00: 0, 0x04: 0, 0x08: 0, 0x0C: 0, 0x14: 0, 0x20: 0, 0x24: 0}
		self.inputs = {}  # pin -> callable() -> 0/1
		self.listeners = {}  # pin -> [callable(level)]
		self.writes = 0

	def odr(self):
		return self.regs[0x14]

	def level(self, pin):
		mode = (self.regs[0x00] >> (2 * pin)) & 3
		if mode == 1:  # output
			return (self.regs[0x14] >> pin) & 1
		src = self.inputs.get(pin)
		if src is not None:
			return src() & 1
		pupd = (self.regs[0x0C] >> (2 * pin)) & 3
		return 1 if pupd == 1 else 0

	def idr(self):
		moder, pupd = self.regs[0x00], self.regs[0x0C]
		out = 0
		v = 0
		for pin in range(16):
			mode = (moder >> (2 * pin)) & 3
			if mode == 1:
				out |= 1 << pin
			elif pin in self.inputs:
				if self.inputs[pin]() & 1:
					v |= 1 << pin
			elif (pupd >> (2 * pin)) & 3 == 1:
				v |= 1 << pin
		return v | (self.regs[0x14] & out)

	def listen(self, pin, fn):
		self.listeners.setdefault(pin, []).append(fn)

	def _set_odr(self, new):
		old = self.regs[0x14]
		self.regs[0x14] = new & 0xFFFF
		changed = old ^ new
		if changed:
			for pin, fns in self.listeners.items():
				if changed >> pin & 1:
					lvl = (new >> pin) & 1
					for fn in fns:
						fn(lvl)

	def read(self, off, size):
		base = off & ~3
		v = self.idr() if base == 0x10 else self.regs.get(base, 0)
		return v >> (8 * (off & 3)) & ((1 << (8 * size)) - 1)

	def write(self, off, size, value):
		self.writes += 1
		base, shift = off & ~3, 8 * (off & 3)
		value = (value << shift) & 0xFFFFFFFF
		if base == 0x18:  # BSRR: set low half, reset high half (a byte/halfword write hits one)
			if size < 4:
				mask = ((1 << (8 * size)) - 1) << shift
				value &= mask
			odr = self.regs[0x14]
			odr |= value & 0xFFFF
			odr &= ~(value >> 16) & 0xFFFF
			self._set_odr(odr)
		elif base == 0x14:
			mask = ((1 << (8 * size)) - 1) << shift
			self._set_odr((self.regs[0x14] & ~mask) | (value & mask))
		elif base == 0x10:
			pass
		else:
			mask = ((1 << (8 * size)) - 1) << shift
			self.regs[base] = (self.regs.get(base, 0) & ~mask) | (value & mask)
			if base == 0x00:
				self._set_odr(self.regs[0x14])  # mode change can expose ODR on a pin


class Timer(object):
	"""General-purpose/advanced timer, up-counting, internal clock only.

	Modelled: CNT derived from the cycle clock, PSC/ARR with preload on update, update interrupt,
	compare interrupts on CC1..CC4, one-pulse mode, EGR.UG, SR write-0-to-clear, PWM duty readout.
	Not modelled: down/center counting, input capture, slave modes, DMA requests, output pins.
	"""

	def __init__(self, machine, nvic, name, irq, clock_hz, bits=16, cc_irq=None):
		self.m, self.nvic, self.name = machine, nvic, name
		self.irq, self.cc_irq = irq, cc_irq if cc_irq is not None else irq
		self.clock_hz = clock_hz
		self.mask = (1 << bits) - 1
		self.r = {0x00: 0, 0x0C: 0, 0x10: 0, 0x28: 0, 0x2C: self.mask, 0x34: 0, 0x38: 0, 0x3C: 0, 0x40: 0}
		self.psc_active, self.arr_active = 0, self.mask
		self.t0 = 0.0  # cycle at which CNT was 0
		self._gen = 0

	def _ratio(self):
		return self.m.hz / self.clock_hz * (self.psc_active + 1)  # CPU cycles per count

	def cnt(self):
		if not self.r[0x00] & 1:
			return self.r.get(0x24, 0)
		n = int((self.m.cycles - self.t0) / self._ratio())
		return n % (self.arr_active + 1)

	def _reload(self, at):
		self.psc_active = self.r[0x28]
		self.arr_active = self.r[0x2C] or 0
		self.t0 = at

	def _arm(self, after=None):
		"""Schedule the next update or compare event strictly after cycle `after` (default: now)."""
		self._gen += 1
		if not self.r[0x00] & 1:
			return
		ref = self.m.cycles if after is None else after
		gen, ratio = self._gen, self._ratio()
		period = (self.arr_active + 1) * ratio
		k = int((ref - self.t0) // period)
		events = []
		for kk in (k, k + 1):
			base = self.t0 + kk * period
			events.append((base + period, "U"))
			for i in range(4):
				ccr = self.r[0x34 + 4 * i]
				if 0 < ccr <= self.arr_active:
					events.append((base + ccr * ratio, i))
		due, what = min((e for e in events if e[0] > ref), key=lambda e: e[0])

		def fire():
			if gen != self._gen:
				return
			if what == "U":
				self.r[0x10] |= 1
				self._reload(due)
				if self.r[0x00] & 8:  # OPM
					self.r[0x00] &= ~1
					self.r[0x24] = 0
				if self.r[0x0C] & 1:
					self.nvic.irq(self.irq)
			else:
				self.r[0x10] |= 1 << (what + 1)
				if self.r[0x0C] & (1 << (what + 1)):
					self.nvic.irq(self.cc_irq)
			self._arm(after=due)

		self.m.schedule(due, fire)

	def duty(self, channel):
		"""PWM mode 1 duty (0..1) for channel 0..3, if the timer is running."""
		if not self.r[0x00] & 1:
			return 0.0
		return min(1.0, self.r[0x34 + 4 * channel] / float(self.arr_active + 1))

	def read(self, off, size):
		base = off & ~3
		if base == 0x24:
			return self.cnt()
		return self.r.get(base, 0) >> (8 * (off & 3)) & ((1 << (8 * size)) - 1)

	def write(self, off, size, value):
		base = off & ~3
		if base == 0x10:  # SR: rc_w0
			self.r[0x10] &= value
			return
		if base == 0x14:  # EGR
			if value & 1:
				self._reload(self.m.cycles)
				self.r[0x10] |= 1
			self._arm()
			return
		if base == 0x24:
			self.r[0x24] = value & self.mask
			self.t0 = self.m.cycles - (value & self.mask) * self._ratio()
			self._arm()
			return
		if base == 0x00:
			was = self.r[0x00] & 1
			self.r[0x00] = value
			if value & 1 and not was:
				self.t0 = self.m.cycles - self.r.get(0x24, 0) * self._ratio()
			elif was and not value & 1:
				self.r[0x24] = self.cnt()
			self._arm()
			return
		self.r[base] = value
		if base == 0x2C and not self.r[0x00] & 0x80:  # ARPE clear: ARR takes effect now
			self.arr_active = value or 0
		if base in (0x0C, 0x2C, 0x34, 0x38, 0x3C, 0x40):
			self._arm()


class Adc(object):
	"""ADC1/2/3 single regular conversion. DR = channels[SQR3 SQ1]() (a 12-bit code).

	Modelled: SWSTART -> EOC after a fixed conversion time, EOC interrupt, continuous mode.
	Not modelled: scan sequences beyond SQ1, injected channels, DMA, analog watchdog.
	"""

	CONVERSION_S = 2e-6
	# Continuous conversion runs at ~500 kHz on hardware. Simulating every conversion would cost
	# one emulator stop per conversion, so continuous mode is modelled as "DR is always fresh"
	# and a DMA target buffer is refreshed every DMA_REFRESH_S instead.
	DMA_REFRESH_S = 1e-3

	def __init__(self, machine, nvic, irq=18):
		self.m, self.nvic, self.irq = machine, nvic, irq
		self.r = {}
		self.channels = {}
		self.conversions = 0
		self.dma = None  # callable() -> (address, count, bytes_per_item) or None
		self._refreshing = False

	def _sample(self):
		ch = self.r.get(0x34, 0) & 0x1F
		src = self.channels.get(ch)
		return (int(src()) if src else 0) & 0xFFF

	def _convert(self):
		self.r[0x4C] = self._sample()
		self.r[0x00] = self.r.get(0x00, 0) | 0x02 | 0x10  # EOC | STRT
		self.conversions += 1
		if self.r.get(0x04, 0) & (1 << 5):
			self.nvic.irq(self.irq)

	def _continuous(self):
		return self.r.get(0x08, 0) & 3 == 3  # ADON | CONT

	def _refresh(self):
		self._refreshing = False
		if not self._continuous():
			return
		value = self._sample()
		self.r[0x4C] = value
		target = self.dma() if self.dma and self.r.get(0x08, 0) & (1 << 8) else None
		if target:
			addr, count, width = target
			self.m.uc.mem_write(addr, value.to_bytes(width, "little") * count)
		self.conversions += 1
		self._refreshing = True
		self.m.schedule_in(self.DMA_REFRESH_S, self._refresh)

	def read(self, off, size):
		base = off & ~3
		if base == 0x4C and self._continuous():
			self.r[0x4C] = self._sample()
		v = self.r.get(base, 0)
		if base == 0x00 and self._continuous():
			v |= 0x12
		if base == 0x4C:
			self.r[0x00] = self.r.get(0x00, 0) & ~0x02  # reading DR clears EOC
		return v >> (8 * (off & 3)) & ((1 << (8 * size)) - 1)

	def write(self, off, size, value):
		base = off & ~3
		if base == 0x00:
			self.r[0x00] = self.r.get(0x00, 0) & value
			return
		self.r[base] = value
		if base == 0x08 and value & (1 << 30) and value & 1:
			self.r[0x08] = value & ~(1 << 30)
			if self._continuous():
				if not self._refreshing:
					self._refresh()
			else:
				self.m.schedule_in(self.CONVERSION_S, self._convert)


class Dma(Register):
	"""DMA1/DMA2 register file with a stream accessor; transfers are done by the requesting model."""

	def stream(self, n):
		base = 0x10 + 0x18 * n
		cr = self.regs.get(base, 0)
		if not cr & 1:
			return None
		count = self.regs.get(base + 4, 0) & 0xFFFF
		width = 1 << ((cr >> 13) & 3)  # MSIZE
		return self.regs.get(base + 0x0C, 0), count, width


class Spi(object):
	"""SPI master. A DR write clocks one frame through whichever attached device is selected.

	Devices: objects with selected() -> bool and transfer(word, bits) -> word.
	Not modelled: slave mode, CRC, DMA, interrupts.
	"""

	def __init__(self, machine, name):
		self.m, self.name = machine, name
		self.r = {0x00: 0, 0x04: 0}
		self.rx = 0
		self.rxne = False
		self.devices = []

	def read(self, off, size):
		base = off & ~3
		if base == 0x08:
			return 0x02 | (0x01 if self.rxne else 0)  # TXE, RXNE; never BSY
		if base == 0x0C:
			self.rxne = False
			return self.rx
		return self.r.get(base, 0)

	def write(self, off, size, value):
		base = off & ~3
		if base == 0x0C:
			bits = 16 if self.r[0x00] & (1 << 11) else 8
			rx = (1 << bits) - 1
			for dev in self.devices:
				if dev.selected():
					rx = dev.transfer(value & ((1 << bits) - 1), bits)
					break
			self.rx, self.rxne = rx, True
		else:
			self.r[base] = value


class Usart(Register):
	def read(self, off, size):
		if off & ~3 == 0x00:
			return 0xC0  # TXE | TC
		return Register.read(self, off, size)


class OtgFs(Register):
	"""USB OTG_FS placeholder: enough for a core reset to complete. Not a USB device model."""

	def read(self, off, size):
		base = off & ~3
		if base == 0x010:  # GRSTCTL: AHB idle, resets self-clear
			return 0x80000000
		if base == 0x014:  # GINTSTS: device mode, nothing pending
			return 0
		return Register.read(self, off, size)
