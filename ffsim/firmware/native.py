# coding=utf-8
"""Optional native block hook that bounds a Unicorn run by executed code size.

Unicorn's `count=` limit installs a per-instruction hook, which slows emulation several times.
This module compiles (once, with the system C compiler) a ten-line block hook that subtracts each
basic block's byte size from a budget and stops emulation when it runs out. The run loop converts
bytes to cycles with `cycles_per_byte`. If no compiler is available, callers fall back to `count=`.

Accuracy cost: an event can be taken up to one basic block late, and cycles are estimated from
code bytes (Thumb-2 mixes 2- and 4-byte instructions) rather than counted per instruction.
"""

import ctypes
import hashlib
import os
import subprocess
import tempfile

SOURCE = r"""
#include <stdint.h>
typedef int (*stop_fn)(void *);
static stop_fn uc_stop;
static int64_t budget;
static uint64_t executed;
void ffsim_init(stop_fn f) { uc_stop = f; }
void ffsim_set_budget(int64_t b) { budget = b; }
int64_t ffsim_budget(void) { return budget; }
uint64_t ffsim_executed(void) { return executed; }
void ffsim_block(void *uc, uint64_t address, uint32_t size, void *user) {
	executed += size;
	budget -= size;
	if (budget <= 0) uc_stop(uc);
}
"""


def _build():
	cache = os.path.join(os.path.expanduser("~"), ".cache", "ffsim")
	os.makedirs(cache, exist_ok=True)
	tag = hashlib.sha256(SOURCE.encode()).hexdigest()[:12]
	lib = os.path.join(cache, "ffsim_block_%s.so" % tag)
	if not os.path.exists(lib):
		with tempfile.NamedTemporaryFile("w", suffix=".c", delete=False) as f:
			f.write(SOURCE)
		subprocess.check_call(["cc", "-O2", "-shared", "-fPIC", "-o", lib + ".tmp", f.name])
		os.replace(lib + ".tmp", lib)
		os.unlink(f.name)
	return ctypes.CDLL(lib)


class BlockBudget(object):
	def __init__(self, uc):
		from unicorn.unicorn_py3.unicorn import uclib
		from unicorn import UC_HOOK_BLOCK
		self.lib = _build()
		self.lib.ffsim_set_budget.argtypes = [ctypes.c_int64]
		self.lib.ffsim_budget.restype = ctypes.c_int64
		self.lib.ffsim_executed.restype = ctypes.c_uint64
		stop = ctypes.cast(uclib.uc_emu_stop, ctypes.c_void_p)
		self.lib.ffsim_init.argtypes = [ctypes.c_void_p]
		self.lib.ffsim_init(stop)
		self._handle = ctypes.c_size_t()
		hook = ctypes.cast(self.lib.ffsim_block, ctypes.c_void_p)
		err = uclib.uc_hook_add(uc._uch, ctypes.byref(self._handle), UC_HOOK_BLOCK, hook, None,
								ctypes.c_uint64(1), ctypes.c_uint64(0))
		if err:
			raise RuntimeError("uc_hook_add failed: %d" % err)
		self._stolen = 0

	def take_stolen(self):
		s, self._stolen = self._stolen, 0
		return s

	def set(self, nbytes):
		self.lib.ffsim_set_budget(int(nbytes))

	def stop_soon(self):
		"""Stop at the next basic-block boundary, after the current instruction has completed.

		Calling uc_emu_stop from inside an MMIO callback leaves PC on the faulting store, so the
		store would run again on restart; ending the budget instead lets the instruction retire.
		"""
		self.set_spent_marker()

	def set_spent_marker(self):
		# Remember how much was left so the run loop can still account the cycles correctly.
		left = self.lib.ffsim_budget()
		if left > 0:
			self._stolen += left
			self.lib.ffsim_set_budget(0)

	def remaining(self):
		return self.lib.ffsim_budget()

	def executed(self):
		return self.lib.ffsim_executed()


def try_block_budget(uc):
	try:
		return BlockBudget(uc)
	except Exception:  # noqa: BLE001 - any failure means "use count= instead"
		return None
