# coding=utf-8
"""Layer (b): System Control Space - NVIC, SCB, SysTick and DWT cycle counter (ARMv7-M).

The registers themselves (0xE000E000 page: NVIC, SCB, SysTick; 0xE0001000 page: DWT) are served
by the native core, core.c, because interrupt delivery has to be checked on every basic block
and Klipper polls CYCCNT in tight loops. Register semantics follow the ARMv7-M Architecture
Reference Manual B3. Two details matter to the firmwares:

  - SysTick LOAD is only sampled at a reload, as on hardware: a VAL write clears the counter and
    the next clock reloads it from LOAD; reaching 0 sets COUNTFLAG (and pends the exception) and
    the next clock reloads again. Klipper's one-shot idiom (LOAD=n; VAL=0; LOAD=0) depends on it.
  - CYCCNT is the simulated cycle clock, live inside a run.

This module is the Python-side view that peripherals and firmware adapters use.
"""

SYSTICK = 15


class Nvic(object):
	def __init__(self, machine):
		self.m = machine
		self._lib = machine.lib

	def set_pending(self, number):
		self._lib.ffsim_set_pending(number)

	def irq(self, irqn):
		self._lib.ffsim_set_pending(16 + irqn)

	def priority(self, number):
		return self._lib.ffsim_priority(number)

	def inject_call(self, vector, args=(), priority=0xFF):
		"""Run vector(args) as if it were an interrupt handler (used by HLE adapters).

		Taken only from thread mode (no handler active), after any pending real exception that
		is deliverable, at `priority` against BASEPRI/PRIMASK."""
		a = (list(args) + [0, 0, 0, 0])[:4]
		self._lib.ffsim_inject(vector, a[0], a[1], a[2], a[3], priority)

	@property
	def hle_queue(self):
		"""Number of injected calls not yet started."""
		return self._lib.ffsim_inject_count()

	@property
	def vtor(self):
		return self._lib.ffsim_vtor()

	def delivered(self, number):
		return self._lib.ffsim_delivered(number)
