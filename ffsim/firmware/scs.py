# coding=utf-8
"""Layer (b): System Control Space - NVIC, SCB, SysTick and DWT cycle counter (ARMv7-M).

Register offsets are relative to 0xE000E000 (NVIC/SCB/SysTick) and 0xE0001000 (DWT), per the
ARMv7-M Architecture Reference Manual B3.
"""

SYSTICK = 15


class Nvic(object):
	def __init__(self, machine, num_irq=82):
		self.m = machine
		self.num_irq = num_irq
		self.enabled = set()  # external IRQ numbers
		self.pending = set()  # exception numbers (>= 2)
		self.active = []
		self.prio = {}  # exception number -> priority byte
		self.vtor = 0
		self.shpr = [0, 0, 0]
		self.hle_queue = []  # (priority, vector, args, done_callback) software-injected calls
		self.delivered = {}

	# external interface used by peripherals
	def set_pending(self, number):
		self.pending.add(number)

	def irq(self, irqn):
		self.set_pending(16 + irqn)

	def priority(self, number):
		if number >= 16:
			return self.prio.get(number, 0)
		if number >= 4:
			return (self.shpr[(number - 4) // 4] >> (8 * (number % 4))) & 0xFF
		return -1

	def inject_call(self, vector, args, priority=0x100 - 1):
		"""Run vector(args) as if it were an interrupt handler (used by HLE adapters)."""
		self.hle_queue.append((priority, vector, args))

	def deliver(self, m):
		if m.interrupts_masked():
			return
		current = m.current_priority()
		best = None
		for number in self.pending:
			if number >= 16 and (number - 16) not in self.enabled:
				continue
			p = self.priority(number)
			if p < current and (best is None or (p, number) < best[:2]):
				best = (p, number)
		if best is not None:
			p, number = best
			self.pending.discard(number)
			self.delivered[number] = self.delivered.get(number, 0) + 1
			m.enter_exception(number, p)
			return
		if self.hle_queue and not m._exc_stack:
			p, vector, args = self.hle_queue.pop(0)
			m.enter_exception(-1, p, vector=vector, args=args)

	def exception_returned(self, number):
		pass

	# register access (offset from 0xE000E000)
	def read(self, off, size):
		if 0x100 <= off < 0x120:  # ISER
			return self._bits(self.enabled, (off - 0x100) * 8)
		if 0x180 <= off < 0x1A0:  # ICER
			return self._bits(self.enabled, (off - 0x180) * 8)
		if 0x200 <= off < 0x220:  # ISPR
			return self._bits({n - 16 for n in self.pending if n >= 16}, (off - 0x200) * 8)
		if 0x400 <= off < 0x400 + self.num_irq:  # IPR, byte addressable
			v = 0
			for i in range(size):
				v |= self.prio.get(16 + off - 0x400 + i, 0) << (8 * i)
			return v
		if off == 0xD00:  # CPUID: Cortex-M4 r0p1
			return 0x410FC241
		if off == 0xD04:  # ICSR
			v = 0
			if SYSTICK in self.pending:
				v |= 1 << 26
			return v
		if off == 0xD08:
			return self.vtor
		if 0xD18 <= off < 0xD24:
			i = (off - 0xD18) // 4
			return self.shpr[i] >> (8 * (off & 3)) & ((1 << (8 * size)) - 1)
		if off in self.m.scb_extra:
			return self.m.scb_extra[off]
		return 0

	def write(self, off, size, value):
		if off in (0xD04, 0xF00) or 0x100 <= off < 0x120 or 0x200 <= off < 0x220:
			self.m.request_stop()  # enabling/pending may make an exception due right now
		if off == 0xF00:  # STIR
			self.pending.add(16 + (value & 0x1FF))
		elif 0x100 <= off < 0x120:
			self._apply(self.enabled.add, value, (off - 0x100) * 8)
		elif 0x180 <= off < 0x1A0:
			self._apply(self.enabled.discard, value, (off - 0x180) * 8)
		elif 0x200 <= off < 0x220:
			self._apply(lambda n: self.pending.add(16 + n), value, (off - 0x200) * 8)
		elif 0x280 <= off < 0x2A0:
			self._apply(lambda n: self.pending.discard(16 + n), value, (off - 0x280) * 8)
		elif 0x400 <= off < 0x400 + self.num_irq:
			for i in range(size):
				self.prio[16 + off - 0x400 + i] = (value >> (8 * i)) & 0xF0
		elif off == 0xD04:  # ICSR
			if value & (1 << 26):
				self.pending.add(SYSTICK)
			if value & (1 << 25):
				self.pending.discard(SYSTICK)
			if value & (1 << 28):
				self.pending.add(14)
		elif off == 0xD08:
			self.vtor = value & 0xFFFFFF80
		elif 0xD18 <= off < 0xD24:
			i, shift = (off - 0xD18) // 4, 8 * (off & 3)
			mask = ((1 << (8 * size)) - 1) << shift
			self.shpr[i] = (self.shpr[i] & ~mask) | ((value << shift) & mask)
		else:
			self.m.scb_extra[off] = value

	@staticmethod
	def _bits(s, first):
		return sum(1 << (n - first) for n in s if first <= n < first + 32)

	@staticmethod
	def _apply(fn, value, first):
		for b in range(32):
			if value >> b & 1:
				fn(first + b)


class SysTick(object):
	"""SysTick counts processor clock cycles (CLKSOURCE=1) or HCLK/8 (CLKSOURCE=0)."""

	def __init__(self, machine, nvic):
		self.m, self.nvic = machine, nvic
		self.ctrl = 0
		self.load = 0
		self.start_cycle = 0.0  # cycle at which the counter last reloaded
		self.countflag = False
		self._gen = 0

	def _period(self):
		div = 1 if self.ctrl & 4 else 8
		return (self.load + 1) * div

	def _arm(self):
		self._gen += 1
		if not (self.ctrl & 1) or self.load == 0:
			return
		gen = self._gen
		due = self.start_cycle + self._period()

		def fire():
			if gen != self._gen:
				return
			self.countflag = True
			self.start_cycle = due
			if self.ctrl & 2:
				self.nvic.set_pending(SYSTICK)
			self._arm()

		self.m.schedule(due, fire)

	def current(self):
		if not (self.ctrl & 1) or self.load == 0:
			return 0
		div = 1 if self.ctrl & 4 else 8
		elapsed = (self.m.cycles - self.start_cycle) / div
		return int(self.load - (elapsed % (self.load + 1))) & 0xFFFFFF

	def read(self, off, size):
		if off == 0x10:
			v = self.ctrl | (0x10000 if self.countflag else 0)
			self.countflag = False
			return v
		if off == 0x14:
			return self.load
		if off == 0x18:
			return self.current()
		if off == 0x1C:  # CALIB: no reference clock, 10 ms not exact
			return 0xC0000000
		return self.nvic.read(off, size)

	def write(self, off, size, value):
		if off == 0x10:
			was = self.ctrl & 1
			self.ctrl = value & 7
			if self.ctrl & 1 and not was:
				self.start_cycle = self.m.cycles
			self._arm()
		elif off == 0x14:
			self.load = value & 0xFFFFFF
			self._arm()
		elif off == 0x18:  # any write clears the counter; next tick reloads
			self.start_cycle = self.m.cycles
			self.countflag = False
			self._arm()
		else:
			self.nvic.write(off, size, value)


class Scs(object):
	"""0xE000E000..0xE000EFFF: SysTick at 0x010, everything else to the NVIC/SCB."""

	def __init__(self, machine):
		machine.scb_extra = {}
		self.nvic = Nvic(machine)
		self.systick = SysTick(machine, self.nvic)
		machine.nvic = self.nvic

	def read(self, off, size):
		if 0x10 <= off < 0x20:
			return self.systick.read(off, size)
		return self.nvic.read(off, size)

	def write(self, off, size, value):
		if 0x10 <= off < 0x20:
			self.systick.write(off, size, value)
		else:
			self.nvic.write(off, size, value)


class Dwt(object):
	"""DWT CYCCNT (offset 0x004) tracks the simulated cycle counter; Klipper uses it as its clock."""

	def __init__(self, machine):
		self.m = machine
		self.ctrl = 0
		self.offset = 0.0

	def read(self, off, size):
		if off == 0x000:
			return self.ctrl
		if off == 0x004:
			return int(self.m.cycles - self.offset) & 0xFFFFFFFF
		return 0

	def write(self, off, size, value):
		if off == 0x000:
			self.ctrl = value
		elif off == 0x004:
			self.offset = self.m.cycles - value
