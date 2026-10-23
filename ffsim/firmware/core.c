/* ffsim native core: the parts of layer (a)/(b) that run once per basic block or per interrupt.
 *
 *   - cycle clock (code-size budget per run, live value for MMIO callbacks)
 *   - NVIC, SCB, SysTick and DWT CYCCNT (ARMv7-M ARM B3), mapped as MMIO from here
 *   - exception entry, tail-chaining and return, checked at every basic-block boundary
 *   - WFI as a sleep until the next event
 *
 * Python (cpu.py, scs.py) drives runs and owns everything else. Unicorn API functions are passed
 * in as pointers at init, so this file needs no Unicorn headers and links against nothing.
 * Register ids are passed in too (see native.py), so they always match the installed Unicorn.
 *
 * All state lives in a core_t, one per Machine, passed to every hook as its user pointer and to
 * every API call as the first argument. C points at the one being served on this thread.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

typedef void uc_engine;
typedef int (*reg_rw_fn)(uc_engine *, int, void *);
typedef int (*reg_batch_fn)(uc_engine *, int *, void **, int);
typedef int (*mem_rw_fn)(uc_engine *, uint64_t, void *, uint64_t);
typedef int (*stop_fn)(uc_engine *);

#define TRAMPOLINE 0x0FFF0000u
#define MAXEXC 256
#define INF 1e300

typedef struct { int number; int prio; uint32_t fp[17]; uint32_t align; } active_t;
typedef struct { uint32_t vector, args[4]; int prio; } inject_t;

typedef struct {
	uc_engine *uc;
	reg_rw_fn reg_read, reg_write;
	reg_batch_fn reg_read_batch, reg_write_batch;
	mem_rw_fn mem_read, mem_write;
	stop_fn stop;
	int R0, R1, R2, R3, R12, SP, LR, PC, XPSR, PRIMASK, BASEPRI, S0, FPSCR;

	/* clock */
	double base_cycles;  /* cycle at the start of the run (moved forward by WFI sleeps) */
	int64_t budget;      /* bytes of code left in this run; <= 0 stops at the next block */
	double cpb;          /* cycles per byte of executed code */
	double run_limit;    /* the run must not pass this cycle */
	double py_next;      /* next Python-side event */
	int running;
	uint64_t executed;
	int64_t run_bytes;   /* code executed since run_begin */
	int stop_reason;     /* 1 = Python event or run limit reached in WFI, 2 = WFI with nothing to wake it */
	double idle_cycles;

	/* NVIC/SCB */
	uint8_t pending[MAXEXC], enabled[MAXEXC], prio[MAXEXC];
	uint32_t shpr[3], vtor, scb_other[1024];
	uint64_t delivered[MAXEXC];
	int pending_any;
	int num_irq;
	active_t active[64];
	int depth;
	inject_t injq[64];
	int injn;

	/* SysTick */
	uint32_t st_ctrl, st_load, st_cur;
	double st_start, st_due;
	int st_countflag;

	/* DWT */
	uint32_t dwt_ctrl;
	double dwt_offset;
} core_t;

static __thread core_t *C;

static double live(void) {
	if (!C->running) return C->base_cycles;
	return C->base_cycles + (double)C->run_bytes * C->cpb;
}

static int exc_prio(int n) {
	if (n >= 16) return C->prio[n];
	if (n >= 4) return (C->shpr[(n - 4) / 4] >> (8 * (n % 4))) & 0xFF;
	return -1;
}

/* ---- SysTick ------------------------------------------------------------------------------- */
static double st_div(void) { return (C->st_ctrl & 4) ? 1.0 : 8.0; }

/* the counter reloads from LOAD at cycle `at`; LOAD is sampled only here */
static void st_reload(double at) {
	C->st_cur = C->st_load;
	C->st_start = at;
	C->st_due = ((C->st_ctrl & 1) && C->st_cur) ? at + C->st_cur * st_div() : INF;
}

static void systick_check(double now) {
	while (C->st_due <= now) {
		double due = C->st_due;
		C->st_countflag = 1;
		if (C->st_ctrl & 2) { C->pending[15] = 1; C->pending_any = 1; }
		st_reload(due + st_div());
	}
}

/* ---- helpers ------------------------------------------------------------------------------- */
static uint32_t rd(int reg) { uint32_t v = 0; C->reg_read(C->uc, reg, &v); return v; }
static void wr(int reg, uint32_t v) { C->reg_write(C->uc, reg, &v); }
static void request_stop(void) { if (C->budget > 0) C->budget = 0; }

/* best deliverable exception number, -1 for an injected call, or 0 */
static int best_pending(int *out_prio) {
	if (!C->pending_any && !C->injn) return 0;
	if (rd(C->PRIMASK) & 1) return 0;
	int cur = C->depth ? C->active[C->depth - 1].prio : 0x100;
	int bp = rd(C->BASEPRI) & 0xFF;
	if (bp && bp < cur) cur = bp;
	int best = 0, bestp = 0x100, any = 0;
	for (int n = 2; n < 16 + C->num_irq; n++) {
		if (!C->pending[n]) continue;
		any = 1;
		if (n >= 16 && !C->enabled[n]) continue;
		int p = exc_prio(n);
		if (p < cur && p < bestp) { best = n; bestp = p; }
	}
	if (!any) C->pending_any = 0;
	if (best) { *out_prio = bestp; return best; }
	if (C->injn && !C->depth && C->injq[0].prio < cur) { *out_prio = C->injq[0].prio; return -1; }
	return 0;
}

static void enter(int number, int p, uint32_t vector, const uint32_t *args) {
	int ids[26];
	uint32_t vals[26];
	void *ptrs[26];
	int k = 0;
	ids[k++] = C->SP; ids[k++] = C->R0; ids[k++] = C->R1; ids[k++] = C->R2; ids[k++] = C->R3;
	ids[k++] = C->R12; ids[k++] = C->LR; ids[k++] = C->PC; ids[k++] = C->XPSR;
	for (int i = 0; i < 16; i++) ids[k++] = C->S0 + i;
	ids[k++] = C->FPSCR;
	for (int i = 0; i < k; i++) { vals[i] = 0; ptrs[i] = &vals[i]; }
	C->reg_read_batch(C->uc, ids, ptrs, k);
	uint32_t sp = vals[0];
	uint32_t align = sp & 4;
	sp = sp - 32 - align;
	C->mem_write(C->uc, sp, &vals[1], 32); /* r0 r1 r2 r3 r12 lr pc xpsr */
	active_t *a = &C->active[C->depth++];
	a->number = number; a->prio = p; a->align = align;
	memcpy(a->fp, &vals[9], 17 * 4);
	if (number > 0) C->delivered[number]++;
	uint32_t w[7] = {sp, TRAMPOLINE | 1, vector | 1, 0, 0, 0, 0};
	int wid[7] = {C->SP, C->LR, C->PC, C->R0, C->R1, C->R2, C->R3};
	void *wp[7];
	if (args) memcpy(&w[3], args, 16);
	for (int i = 0; i < 7; i++) wp[i] = &w[i];
	C->reg_write_batch(C->uc, wid, wp, args ? 7 : 3);
	wr(C->PC, vector | 1); /* a plain reg_write is what redirects execution from inside a hook */
}

static int try_deliver(void) {
	int p = 0;
	int n = best_pending(&p);
	if (!n) return 0;
	if (n > 0) {
		uint32_t vec = 0;
		C->mem_read(C->uc, C->vtor + 4 * n, &vec, 4);
		C->pending[n] = 0;
		enter(n, p, vec, 0);
	} else {
		inject_t in = C->injq[0];
		memmove(&C->injq[0], &C->injq[1], sizeof(inject_t) * (--C->injn));
		enter(-1, in.prio, in.vector, in.args);
	}
	return 1;
}

/* ---- hooks --------------------------------------------------------------------------------- */
static void block_hook(uc_engine *uc, uint64_t address, uint32_t size, void *user) {
	C = (core_t *)user;
	C->executed += size;
	C->run_bytes += size;
	C->budget -= size;
	double now = live();
	if (C->st_due <= now) systick_check(now);
	if ((C->pending_any || C->injn) && try_deliver()) return;
	if (C->budget <= 0) C->stop(uc);
}

static void exc_return(uc_engine *uc, uint64_t address, uint32_t size, void *user) {
	C = (core_t *)user;
	if (!C->depth) return;
	active_t a = C->active[--C->depth];
	uint32_t sp = rd(C->SP);
	uint32_t frame[8];
	C->mem_read(C->uc, sp, frame, 32);
	int ids[26]; uint32_t vals[26]; void *ptrs[26]; int k = 0;
	ids[k] = C->R0; vals[k++] = frame[0]; ids[k] = C->R1; vals[k++] = frame[1];
	ids[k] = C->R2; vals[k++] = frame[2]; ids[k] = C->R3; vals[k++] = frame[3];
	ids[k] = C->R12; vals[k++] = frame[4]; ids[k] = C->LR; vals[k++] = frame[5];
	ids[k] = C->XPSR; vals[k++] = frame[7]; ids[k] = C->SP; vals[k++] = sp + 32 + a.align;
	for (int i = 0; i < 16; i++) { ids[k] = C->S0 + i; vals[k++] = a.fp[i]; }
	ids[k] = C->FPSCR; vals[k++] = a.fp[16];
	for (int i = 0; i < k; i++) ptrs[i] = &vals[i];
	C->reg_write_batch(C->uc, ids, ptrs, k);
	wr(C->PC, frame[6] | 1);
	if (C->pending_any || C->injn) try_deliver(); /* tail-chain */
}

static void wfi_hook(uc_engine *uc, uint64_t address, uint32_t size, void *user) {
	C = (core_t *)user;
	for (;;) {
		int p;
		if (best_pending(&p)) {
			wr(C->PC, (uint32_t)(address + 2) | 1); /* resume after the WFI */
			try_deliver();
			return;
		}
		double now = live();
		double wake = C->st_due;
		if (C->py_next < wake) wake = C->py_next;
		if (C->run_limit < wake) wake = C->run_limit;
		if (wake >= INF) { C->stop_reason = 2; break; }
		if (wake > now) { C->idle_cycles += wake - now; C->base_cycles += wake - now; }
		if (C->st_due <= wake) { systick_check(wake); continue; }
		C->stop_reason = 1; /* a Python event or the run limit comes first */
		break;
	}
	/* Stay on the WFI: the PC write makes Unicorn leave the block without executing it, and the
	 * next run starts here, so this hook re-evaluates after the Python event has fired. */
	C->budget = 0;
	wr(C->PC, (uint32_t)address | 1);
	C->stop(uc);
}

/* ---- SCS MMIO (0xE000E000 page) and DWT (0xE0001000 page) ----------------------------------- */
static void set_bits(uint8_t *arr, int first, uint32_t v, uint8_t val) {
	for (int b = 0; b < 32; b++)
		if ((v >> b & 1) && 16 + first + b < MAXEXC) arr[16 + first + b] = val;
}
static uint32_t get_bits(const uint8_t *arr, int first) {
	uint32_t v = 0;
	for (int b = 0; b < 32; b++) if (16 + first + b < MAXEXC && arr[16 + first + b]) v |= 1u << b;
	return v;
}

static uint64_t scs_read(uc_engine *uc, uint64_t off, unsigned size, void *user) {
	C = (core_t *)user;
	double now = live();
	if (off == 0x10) {
		systick_check(now);
		uint32_t v = C->st_ctrl | (C->st_countflag ? 0x10000 : 0);
		C->st_countflag = 0;
		return v;
	}
	if (off == 0x14) return C->st_load;
	if (off == 0x18) {
		systick_check(now);
		if (!(C->st_ctrl & 1) || !C->st_cur) return 0;
		double v = C->st_cur - (int64_t)((now - C->st_start) / st_div());
		return v < 0 ? 0 : ((uint32_t)v & 0xFFFFFF);
	}
	if (off == 0x1C) return 0xC0000000; /* CALIB: no reference clock, TENMS not exact */
	if (off >= 0x100 && off < 0x120) return get_bits(C->enabled, (off - 0x100) * 8);
	if (off >= 0x180 && off < 0x1A0) return get_bits(C->enabled, (off - 0x180) * 8);
	if (off >= 0x200 && off < 0x220) return get_bits(C->pending, (off - 0x200) * 8);
	if (off >= 0x280 && off < 0x2A0) return get_bits(C->pending, (off - 0x280) * 8);
	if (off >= 0x400 && off < 0x400 + (uint64_t)C->num_irq) {
		uint64_t v = 0;
		for (unsigned i = 0; i < size && 16 + off - 0x400 + i < MAXEXC; i++)
			v |= (uint64_t)C->prio[16 + off - 0x400 + i] << (8 * i);
		return v;
	}
	if (off == 0xD00) return 0x410FC241; /* CPUID: Cortex-M4 r0p1 */
	if (off == 0xD04) {                  /* ICSR */
		uint32_t v = 0;
		if (C->pending[15]) v |= 1u << 26;
		if (C->depth && C->active[C->depth - 1].number > 0) v |= (uint32_t)(C->active[C->depth - 1].number & 0x1FF);
		return v;
	}
	if (off == 0xD08) return C->vtor;
	if (off >= 0xD18 && off < 0xD24)
		return (C->shpr[(off - 0xD18) / 4] >> (8 * (off & 3))) & (uint32_t)((1ull << (8 * size)) - 1);
	if (off < 4096) return C->scb_other[off / 4];
	return 0;
}

static void scs_write(uc_engine *uc, uint64_t off, unsigned size, uint64_t value, void *user) {
	C = (core_t *)user;
	double now = live();
	uint32_t v = (uint32_t)value;
	if (off == 0x10) {
		uint32_t was = C->st_ctrl & 1;
		systick_check(now);
		C->st_ctrl = v & 7;
		if ((C->st_ctrl & 1) && !was) st_reload(now + st_div());
		else if (!(C->st_ctrl & 1)) C->st_due = INF;
		request_stop();
		return;
	}
	if (off == 0x14) { C->st_load = v & 0xFFFFFF; return; } /* takes effect at the next reload */
	if (off == 0x18) { C->st_countflag = 0; st_reload(now + st_div()); return; }
	if (off >= 0x100 && off < 0x120) { set_bits(C->enabled, (off - 0x100) * 8, v, 1); request_stop(); return; }
	if (off >= 0x180 && off < 0x1A0) { set_bits(C->enabled, (off - 0x180) * 8, v, 0); return; }
	if (off >= 0x200 && off < 0x220) {
		set_bits(C->pending, (off - 0x200) * 8, v, 1);
		C->pending_any = 1;
		request_stop();
		return;
	}
	if (off >= 0x280 && off < 0x2A0) { set_bits(C->pending, (off - 0x280) * 8, v, 0); return; }
	if (off >= 0x400 && off < 0x400 + (uint64_t)C->num_irq) {
		for (unsigned i = 0; i < size && 16 + off - 0x400 + i < MAXEXC; i++)
			C->prio[16 + off - 0x400 + i] = (v >> (8 * i)) & 0xF0;
		return;
	}
	if (off == 0xD04) {
		if (v & (1u << 26)) { C->pending[15] = 1; C->pending_any = 1; request_stop(); }
		if (v & (1u << 25)) C->pending[15] = 0;
		if (v & (1u << 28)) { C->pending[14] = 1; C->pending_any = 1; request_stop(); }
		return;
	}
	if (off == 0xD08) { C->vtor = v & 0xFFFFFF80; return; }
	if (off >= 0xD18 && off < 0xD24) {
		int i = (off - 0xD18) / 4, sh = 8 * (off & 3);
		uint32_t mask = (uint32_t)(((1ull << (8 * size)) - 1) << sh);
		C->shpr[i] = (C->shpr[i] & ~mask) | ((v << sh) & mask);
		return;
	}
	if (off == 0xF00) { /* STIR */
		if (16 + (v & 0x1FF) < MAXEXC) { C->pending[16 + (v & 0x1FF)] = 1; C->pending_any = 1; request_stop(); }
		return;
	}
	if (off < 4096) C->scb_other[off / 4] = v;
}

static uint64_t dwt_read(uc_engine *uc, uint64_t off, unsigned size, void *user) {
	C = (core_t *)user;
	if (off == 0) return C->dwt_ctrl;
	if (off == 4) return (uint32_t)(uint64_t)(live() - C->dwt_offset);
	return 0;
}

static void dwt_write(uc_engine *uc, uint64_t off, unsigned size, uint64_t value, void *user) {
	C = (core_t *)user;
	if (off == 0) C->dwt_ctrl = (uint32_t)value;
	else if (off == 4) C->dwt_offset = live() - (double)(uint32_t)value;
}

/* ---- Python API ------------------------------------------------------------------------------ */
void *ffsim_new(uc_engine *uc, void *rr, void *rw, void *rrb, void *rwb, void *mr, void *mw, void *stop,
				const int *ids) {
	core_t *c = calloc(1, sizeof(core_t));
	if (!c) return 0;
	c->uc = uc;
	c->reg_read = (reg_rw_fn)rr; c->reg_write = (reg_rw_fn)rw;
	c->reg_read_batch = (reg_batch_fn)rrb; c->reg_write_batch = (reg_batch_fn)rwb;
	c->mem_read = (mem_rw_fn)mr; c->mem_write = (mem_rw_fn)mw; c->stop = (stop_fn)stop;
	c->R0 = ids[0]; c->R1 = ids[1]; c->R2 = ids[2]; c->R3 = ids[3]; c->R12 = ids[4]; c->SP = ids[5];
	c->LR = ids[6]; c->PC = ids[7]; c->XPSR = ids[8]; c->PRIMASK = ids[9]; c->BASEPRI = ids[10];
	c->S0 = ids[11]; c->FPSCR = ids[12];
	c->cpb = 1.0 / 2.6;
	c->run_limit = c->py_next = c->st_due = INF;
	c->num_irq = 82;
	return c;
}

void ffsim_free(void *ctx) { free(ctx); }

void *ffsim_hook_ptr(int which) {
	switch (which) {
	case 0: return (void *)block_hook;
	case 1: return (void *)exc_return;
	case 2: return (void *)wfi_hook;
	case 3: return (void *)scs_read;
	case 4: return (void *)scs_write;
	case 5: return (void *)dwt_read;
	case 6: return (void *)dwt_write;
	}
	return 0;
}

#define API(ctx) (C = (core_t *)(ctx))

void ffsim_set_cpb(void *ctx, double v) { API(ctx); C->cpb = v; }
void ffsim_run_begin(void *ctx, double base, int64_t want_bytes, double limit, double next_py) {
	API(ctx);
	C->base_cycles = base; C->budget = want_bytes; C->run_limit = limit; C->py_next = next_py;
	C->running = 1; C->stop_reason = 0; C->run_bytes = 0;
	if (C->st_due <= base) systick_check(base);
}
double ffsim_run_end(void *ctx) {
	API(ctx);
	double end = live();
	C->running = 0;
	C->base_cycles = end;
	return end;
}
double ffsim_live(void *ctx) { API(ctx); return live(); }
void ffsim_stop_soon(void *ctx) { API(ctx); request_stop(); }
int ffsim_stop_reason(void *ctx) { API(ctx); return C->stop_reason; }
void ffsim_set_pending(void *ctx, int n) {
	API(ctx);
	if (n >= 0 && n < MAXEXC) { C->pending[n] = 1; C->pending_any = 1; request_stop(); }
}
int ffsim_pending_any(void *ctx) { API(ctx); return C->pending_any || C->injn; }
int ffsim_is_pending(void *ctx, int n) { API(ctx); return n >= 0 && n < MAXEXC && C->pending[n]; }
void ffsim_inject(void *ctx, uint32_t vector, uint32_t a0, uint32_t a1, uint32_t a2, uint32_t a3, int p) {
	API(ctx);
	if (C->injn < 64) {
		inject_t *in = &C->injq[C->injn++];
		in->vector = vector; in->args[0] = a0; in->args[1] = a1; in->args[2] = a2; in->args[3] = a3;
		in->prio = p;
		request_stop();
	}
}
int ffsim_inject_count(void *ctx) { API(ctx); return C->injn; }
int ffsim_priority(void *ctx, int n) { API(ctx); return n >= 0 && n < MAXEXC ? exc_prio(n) : -1; }
int ffsim_depth(void *ctx) { API(ctx); return C->depth; }
uint64_t ffsim_delivered(void *ctx, int n) { API(ctx); return n >= 0 && n < MAXEXC ? C->delivered[n] : 0; }
uint32_t ffsim_vtor(void *ctx) { API(ctx); return C->vtor; }
void ffsim_set_vtor(void *ctx, uint32_t v) { API(ctx); C->vtor = v; }
int ffsim_deliver_now(void *ctx) { API(ctx); return try_deliver(); }
double ffsim_idle(void *ctx) { API(ctx); return C->idle_cycles; }
uint64_t ffsim_executed(void *ctx) { API(ctx); return C->executed; }
double ffsim_systick_due(void *ctx) { API(ctx); return C->st_due; }
void ffsim_set_num_irq(void *ctx, int n) { API(ctx); if (n > 0 && n <= MAXEXC - 16) C->num_irq = n; }
