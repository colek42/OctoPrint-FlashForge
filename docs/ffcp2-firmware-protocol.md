# FlashForge Creator Pro 2: USB G-code protocol (stock firmware V1.8 20210221)

Reverse-engineered from the vendor's public image `creatorp2_v1.8_20210221.bin`
(872,156 bytes). Static analysis only; the image was never executed and nothing
here was checked against a physical printer.

## How to read this document

- Every claim carries a firmware address: a function entry (`FUN_xxxxxxxx`), a
  RAM variable (`0x2000xxxx`) or a string literal.
- **CONFIRMED** means I read the decompiled (and where it mattered, the
  disassembled) code that implements it.
- **INFERRED** means it rests on strings, naming, a function I did not fully
  read, or reasoning about hardware behaviour. Treat these as hypotheses.
- Hex values for numeric G/M codes are what appears in the code
  (`0x259` = 601 and so on).

### Method and its limits

- Load base **0x08010000**, CONFIRMED: scoring every aligned word in the image
  as a pointer into a NUL-preceded printable string gives 51 hits at 0x08010000
  against at most 10 for any other 4 KB-aligned base from 0x08000000 to
  0x0802F000. The reset vector 0x0802C311 lands on code at that base, and
  radare2 annotates literal-pool loads (e.g. `ldr r0,[0x08015010] = 0x20000241`)
  that agree with the decompiler.
- Most strings are reached by PC-relative `ADR`, not absolute pointers, which
  is why absolute pointer hits are few.
- Tools: Ghidra 12.2 headless (decompiler rebuilt for arm64 from the shipped
  sources), radare2 6.2.4 for spot disassembly. Project, scripts and the full
  decompiler dump live in
  `/private/tmp/claude-501/-Users-nkennedy-proj-judge/e1b7c5d9-039e-41f0-9e34-8556d47d430c/scratchpad/ffcp2-re/`
  (`d.c` is the decompile with every literal-pool word annotated as `{value}`).
- Function discovery: Ghidra auto-analysis plus a pass that seeded functions at
  every `PUSH {..,lr}` in undefined bytes (1,959 functions). Some callers are
  invisible: function-pointer tables (USB class callbacks, USB descriptors) live
  in the compressed RW init data and were not decoded. Where a conclusion
  depends on "no other caller/writer exists", it is INFERRED from a pointer-literal
  search, not proven.

## Architecture in one paragraph

Bare-metal superloop, no RTOS (CONFIRMED, `FUN_0801033e`):

```
loop forever:
    FUN_08015358()   host comm: read bytes, assemble a line, dispatch or enqueue
    FUN_08012aaa()   SD/script reader + command executor (FUN_0801177a)
    FUN_08015a7c()   heaters, 10 ms UI tick (FUN_0801bf32)
    FUN_080183e6()
    FUN_080328ce()   IWDG reload (0x40003000 = 0xAAAA)
```

Timebase: SysTick at SystemCoreClock/10000 (100 us), ISR `FUN_08016e5a` adds
100 to `0x20000250`, so all firmware timers are in **microseconds** (CONFIRMED,
reload write in the SysTick setup, `FUN_08015944` reads the counter).

Host bytes flow through four stages:

1. **USB OUT callback** `FUN_08029272(buf, len)` (INFERRED to be the CDC/bulk
   DataOut callback: it takes a packet and length, its only output is the ring
   below, and no static caller exists because it is reached through a callback
   table). Filters packets and pushes bytes into a **128-byte ring**
   `0x20009958` (`FUN_08028bb0(...,0x80)` in `FUN_08028adc`).
2. **Line reader** `FUN_0801526c` builds one line in `0x200010b4`.
3. **Immediate dispatcher** `FUN_08014c68` answers a fixed set of M-codes on
   the spot.
4. Everything else is **enqueued** by `FUN_08014252` into a 10-slot queue
   (`0x20000b9c`, 0x60 bytes per slot) and later run by the **executor**
   `FUN_0801177a`, which sends `ok` after it acts.

Output goes through `FUN_08028fa4(channel, str)`. Only channel 1 transmits
(USB IN EP 0x81); any other channel value silently discards the text
(CONFIRMED, `FUN_08028fa4` and `FUN_08028b26`).

---

## Q7. Connection lifecycle (M601 / M602 / timeout)

Control variable `0x20000241` ("channel"): 0 = nobody, 1 = USB host,
2 = a second channel that never transmits.

### Before M601 (channel 0)

- CONFIRMED (`FUN_08014c68`, first branch): the dispatcher only acts on
  **M601** and **M615** and swallows every other line, including G-codes. Nothing
  is enqueued.
- CONFIRMED (`FUN_0801526c`): the `CMD xxx Received.` echo is sent to channel 0
  and therefore discarded. **A printer that is not under control returns no
  bytes at all**, for any command except M601.
- M615 runs `FUN_0801402c` silently (it zeroes a counter at `0x20000110+0xDE`;
  INFERRED to be the total print-time counter, it is also called at boot if a
  magic file exists on `1:`, `FUN_08015826`). M615 does nothing once in control:
  it is then just an unknown queued M-code.

### M601 `FUN_080142de` (CONFIRMED)

Parses `S` with `strchr(line,'S')` + strtod. Let `s` = S value (0 if absent).

| channel before | `s` | action | bytes sent |
|---|---|---|---|
| 0 | 0 or absent | channel = 1, `0x20000379`=1 (UI "USB connected") | `CMD M601 Received.\r\nControl Success.\r\nok\r\n` (string `0x08014528`, one transfer) |
| 0 | non-zero | channel = 2 | the same string, but to channel 2, so **nothing** reaches the host |
| 1 | 0 | none | echo `CMD M601 Received.\r\n`, then `Control Success.\r\nok\r\n` |
| 1 | non-zero | none | echo, then `Control failed.\r\nok\r\n` (`0x08014554`) |

It also starts a 10,000,000 us software timer at `0x20001074`. Nothing ever
tests that timer (only `FUN_080142de` starts it and `FUN_08014388` stops it;
CONFIRMED by xref). It is dead: **there is no 10-second M601 lease.**

Channel 2 is reset to 0 at the top of every `FUN_08015358` pass (CONFIRMED), and
while channel is 2 the USB callback discards all traffic except a packet
shorter than 11 bytes starting `~M601`, which gets
`CMD M601 Received.\r\nControl failed.\r\nok\r\n` (`0x080293f4`). Net effect
over USB: **`M601 S1` produces no reply and does not take control.** Use `S0`.

### M602 `FUN_08014388` (CONFIRMED)

Sends `Control Release.\r\nok\r\n` (after the `CMD M602 Received.\r\n` echo),
sets channel 0, clears `0x20000379`, stops the dead timer. M602 sent while not
in control is silently ignored.

### Inactivity drop (CONFIRMED logic, INFERRED wall-clock value)

In `FUN_08015358`:

```
if (line_ready)                 idle = 0          // 0x20000210
if (usb_rx_flag == 1)           idle = 0, usb_rx_flag = 0   // 0x200001f4
else                            idle += 1
if (idle > 0x9FFFF && usb_rx_flag == 0 && channel == 1) {
    channel = 0; 0x20000379 = 0;
    if (receiving_file /*0x2000027a*/) { abort receive; f_close(0x200015d8);
        FUN_08034e50(path) /* INFERRED f_unlink: partial upload deleted */; UI }
}
```

- `usb_rx_flag` is set by the USB callback on **every OUT packet** while channel
  is not 2, before the `~` check (CONFIRMED, `FUN_08029272`). Any packet, even
  one the firmware then discards, resets the counter.
- The threshold is **655,360 superloop iterations**, not a time. No message is
  sent when control drops. Afterwards every command is silently swallowed (see
  "Before M601").
- Wall-clock value: unknown without hardware. My estimate: an idle pass through
  the five superloop calls is on the order of 1,000 cycles at 168 MHz, about
  6 us, which puts the drop at roughly **3 to 5 s when idle**, and longer when
  the UI tick (every 10 ms) does heavy rendering. That matches the plugin's
  empirical "every 4 s" comment. INFERRED; measure it before relying on it.
  A host keepalive at 1 s or faster has margin under any plausible loop speed.

---

## Transport details (Q9 partly)

### USB OUT filter `FUN_08029272` (CONFIRMED)

When not receiving a file (`0x2000027a == 0`):

- A packet is accepted only if its first byte is `~` **or** the previous packet
  was exactly 64 bytes (continuation flag `0x200004c4`). Anything else is
  dropped silently.
- Accepted bytes are pushed into the 128-byte ring one at a time. When the ring
  is full, `FUN_08028bbc` fails and **the rest of the packet is dropped
  silently**. No error, no resend.
- The `~` stays in the line. Multiple `~cmd\r\n` in one packet are fine.

When receiving a file, see Q5.

### Line reader `FUN_0801526c` (CONFIRMED, read in disassembly)

- Terminators: `\r` or `\n`. Each is a terminator, so `\r\n` produces one line
  plus one empty line (empty lines are ignored unless a comment flag is set).
- Max line: when a non-terminator byte arrives with 95 (`0x5F`) bytes already
  stored, the line is cut there and processed; the remaining bytes start the
  next line. Count includes the leading `~`.
- On every non-empty line it sends `CMD <name> Received.\r\n`, where `<name>`
  is up to four bytes starting at index 1 (after the `~`), stopping at a space
  (string `0x080155c0`). Sent before the command runs.
- **Inline comments kill the command.** `;` sets a comment flag
  (`0x20000238`) and bytes after it are not stored. At the terminator, if the
  flag is set, the line is echoed but **never marked ready** and the flag is
  left set. The next empty line (the `\n` of `\r\n`) prints `MSG Received.\r\n`
  and clears the flag. Result for `~G1 X10 ;c\r\n`: echo `CMD G1 Received.`,
  then `MSG Received.`, no execution, **no ok**. With LF-only endings the
  following line is swallowed too.
- While a line is pending (`0x20000230`) the reader consumes no more bytes from
  the ring.

### Line numbers and checksums

Not supported. `FUN_080156ca` ("Resend: %lu") and `FUN_08015d54` (an N/`*`
parser that writes lines to a file) have no callers in the analysed code
(INFERRED dead). `N12 G1 X1*45` is parsed as a G1; `N` is read into
`0x20000098` by `FUN_080106f4`, the checksum is ignored.

### Output timing caveat (INFERRED)

`FUN_08028fa4(1, s)` calls `FUN_0802e176(dev, 0x81, s, strlen(s))`, the ST
`DCD_EP_Tx`, which programs the endpoint and returns without waiting
(`FUN_0802d8ca`, non-DMA path enables the TX-FIFO-empty interrupt). Responses
are built in stack buffers and several are sent back to back (M119 sends three,
M115 seven). If the host is slow to poll, a second transfer can be started
before the first drained. I did not prove corruption happens, but it is a
plausible source of the truncated or merged responses the plugin works around.
Also, a response whose length is an exact multiple of 64 gets no short packet;
the firmware special-cases exactly one of these (`Writing to file:` with an
extra space when the line is 64 bytes, `FUN_080143ea`), which shows the vendor
hit it.

---

## Q1. Command set over USB

Routing depends on the first `M` in the line (`strchr`, `FUN_0803800c`), then
on the first `G` in the executor. A line with neither is "unknown".

### A. Answered immediately by the dispatcher `FUN_08014c68` (CONFIRMED)

Bytes below follow the `CMD <name> Received.\r\n` echo.

| Cmd | Handler | Behaviour | Response |
|---|---|---|---|
| M23 `<path>` | `FUN_08014868` then `FUN_08014768` | open + **start printing** (Q5) | `File opened: <path> Size: <n>\r\n` `File selected\r\nok\r\n`, or `File open failed.\r\nok\r\n`, or `Disk read error.\r\nok\r\n` |
| M24 | inline | resume if paused (`FUN_080146ac(0,0)`) | `ok\r\n` (sent before acting) |
| M25 | inline | pause, **only if MoveMode == MOVING and no cal-pad job** | `ok\r\n` (always, even when ignored) |
| M26 | inline | cancel if not mid pause/resume transition (`FUN_080141ee`) | `ok\r\n` |
| M27 | inline | SD progress | `SD printing byte <a>/<b>\r\nok\r\n` (Q5) |
| M28 `<size> <path>` | `FUN_080143ea` | open file for upload (Q5) | see Q5 |
| M29 | `FUN_080143b4` | close upload file | `Done saving file.\r\n` then `ok\r\n` |
| M105 | `FUN_08014bf8` | temps | Q8 |
| M112 | inline | `FUN_0802b854` (INFERRED stop steppers), `FUN_080103b0` (clear queue, MoveMode=READY) | `ok\r\n` |
| M114 | inline | commanded position `0x20000ff4[0..4]` | `X:%g Y:%g Z:%g A:%g B:%g\r\nok\r\n` |
| M115 | inline | identity | Q8 |
| M119 | `FUN_0801490e` | status | Q6 |
| M502 | inline | value of `0x2000021c` (INFERRED filament sensor state) | `FILAMENT: %d \r\nok\r\n` (note the space before `\r`) |
| M504 | inline | writes 0 to config word `*(0x200000c0)+0xFC` via `FUN_08013696` | **nothing, no ok** |
| M600 | `FUN_080143a6` | nothing | `succeed\r\nok\r\n` |
| M601 / M602 | above | | |
| M650 | inline | two config floats at `0x200001ca-0x9A/-0x96` (INFERRED XY calibration offsets) | `X: %g Y: %g\r\nok\r\n` |
| M651 | inline | position from step counters / steps-per-mm | `X:%g Y:%g Z:%g\r\nok\r\n` |

M20, M21 and M30 do not exist. There is no file listing command.

### B. Queued, executed by `FUN_0801177a`, then `ok` (CONFIRMED)

The `ok` for a queued command is sent by `FUN_080106b6` as **`ok\n` (LF only,
format `"%s\n"` at `0x080107e0`)**, to the channel recorded when the line was
enqueued (`0x20001114[slot]`). It is suppressed when the slot came from SD or a
canned script (`0x20000f5c[slot]=1`) or when MachineStatus is
BUILDING_FROM_SD(2) or BUILDING_ONBOARD(5).

The executor runs only when MoveMode (`0x20000089`) is READY (0) and the queue
is non-empty (`FUN_08012aaa` tail). "Waits idle" below means the handler
returns without consuming the slot until the motion planner ring
(`0x200005b1 == 0x200005b2`, `FUN_08029b72`) is empty, so its `ok` arrives
only after all earlier motion has finished.

| Cmd | Handler | Parameters / behaviour |
|---|---|---|
| G0, G1 | `FUN_080109f0` / `FUN_080106f4` | X Y Z E F N. Sets MoveMode MOVING. In IDEX dual mode the target X becomes X + 66.0 (`FUN_0804b79a` is a float add, constant `0x42840000`) and E drives both extruders unless runout flags `0x20000498/9` are set. ok after the move is planned, not completed (INFERRED, planner not fully read). |
| G4 | `FUN_08010b8a` | P ms or S s. MoveMode DELAY until timer `0x20000fa0` expires. |
| G28 | `FUN_08010bde` | waits idle; homes; MoveMode HOMING; enables both X carriages. |
| G90 | inline | clears relative flag `0x200006ac`. |
| **G91** | none | **no-op**: jumps straight to ok (CONFIRMED in disassembly at `0x08011792`). Nothing sets `0x200006ac`, so relative mode is impossible. |
| G92 | `FUN_08010cd2` | set position. |
| G93 | `FUN_08010cd2` | waits idle, then same as G92. |
| G161 | none | no-op. |
| G162 | `FUN_080116a4` | home to max. |
| other G | none | ok, no action. |
| M0, M1 | `FUN_08011210` | P seconds (0 = no timeout). MoveMode WAIT_ON_BUTTON until UI button or timeout. |
| M6 | `FUN_08010ede` | Q3 |
| M7 | `FUN_08010f16` | Q3 |
| M17 | `FUN_08010dea(1)` | enable steppers. |
| M18 | `FUN_08010dea(0)` | waits idle; disable steppers. |
| M104 | `FUN_08010fa6` | S temp, T tool (T1 = right/second, anything else = T0). Clamped 0..240 (`FUN_08017c98`). May be deferred while the bed is still heating (`0x2000004c`, INFERRED power sharing). |
| M106 | inline | fan on (GPIOF13 / GPIOB0 by active tool; both in dual mode). S ignored. |
| M107 | inline | both fans off. |
| M108 | inline | Q4 |
| M109 | inline | **waits idle and does nothing else** (Q2, Q3). |
| M110 | inline | Q4 |
| M111 | `FUN_080114b8` | Q2 |
| M118 | `FUN_08011266` | FlashPrint job header: X Y Z (fit check), first T -> `0x20000044` (heater M6 waits on), first T != last T -> dual-tool job flag `0x2000000e`, D1/D2 -> cal-pad job. |
| M132 | `FUN_08010f4e` | load home offsets into the planner position. Parameters ignored. |
| M140 | `FUN_0801104a` | S temp, clamped 0..120. Ignored if no heated bed is configured (`FUN_0801360c`). |
| **M146** | none | **no-op** (CONFIRMED at `0x0801182e`). LEDs are not host-controllable. |
| M147 | `FUN_08011112` | F, L (INFERRED buzzer / LED effect, `FUN_0801838a`). |
| M148 | `FUN_08011154` | S 0..8 (INFERRED buzzer/LED pattern). |
| M201, M204, M205, M206, M221, M301, M304, M612, M999 | none | accepted, no-op. |
| M203 | inline | X Y Z E max feed into the axis table `0x2000a29c`. |
| M220 | inline | S -> `0x200006a4` (INFERRED speed factor). |
| M610 | `FUN_080111f8` | string argument at offset 5 (`FUN_08013a8a`, not read). |
| M611 | `FUN_08011198` | S byte (`FUN_08013940`, not read). |
| M906 | `FUN_08010e9c` | waits idle; R and L -> `FUN_080171ac` (INFERRED X-carriage motor currents; ignored during cal-pad jobs). |
| M907 | `FUN_08049d00` | waits on a condition, not read. |
| any other M | none | ok, no action. **This includes M190, M20, M21, M82, M83, M84, M400.** |
| no G and no M (e.g. `T1`) | inline | `echo:Unknown command: "~T1"\r\n` then `ok\n`. |

### C. Silently dropped (CONFIRMED, `FUN_08014252`)

Any line that would be queued is **discarded with no ok** while
- an upload is in progress (`0x2000027a`), or
- an SD/onboard file is open for printing (`0x20000278`).

When the queue already holds 10 lines the enqueue returns 0, the line stays
pending and the reader stops; see Q9.

---

## Q2. IDEX duplicate and mirror

### What sets the mode (CONFIRMED)

- **`M111 Tn`** (`FUN_080114b8`, executor, accepted from USB): waits idle, then
  `0x20000013 = n`, `0x20000008 = 1` (dual active), both carriage-enable flags
  `0x20000001 = 0x20000002 = 1`, and resets the X position bookkeeping to T0.
  Without `T` it does nothing except clear `0x20000014`.
- In the X step routine `FUN_0804a3a8`, with both carriages enabled, the second
  carriage (GPIOC pin 2 step, pin 3 dir) is stepped on every X step. Its
  position counter `0x20000688` moves **opposite** to X1 when
  `0x20000013 == 1` and **with** X1 when `0x20000013 == 2`.

So: **`M111 T1` = mirror, `M111 T2` = duplicate.** The mirror/duplicate
labelling rests on the sign of `0x20000688` relative to `0x20000684` (T0's X
counter, per `FUN_080113ec`). That reading is solid but I did not trace the
counters to physical carriage direction, so call the labels CONFIRMED in code,
INFERRED physically.

### What M109 does (CONFIRMED)

- From the host: M109 is a queued command whose handler only waits for motion
  to finish (`FUN_0801177a`, case `0x6d`). **`M109 T1`/`M109 T2` streamed over
  USB do not change the IDEX mode.**
- From an SD file or canned script only: the SD/script line reader
  `FUN_0801287a` pattern-matches raw characters as they are read: `line[0..3]
  == "M109"`, `line[5] == 'T'`, `line[6]` = `1` or `2`. It sets
  `0x20000010 = 1` or `2` (any other M109 sets 0) and pauses SD reading
  (`0x2000000b`). The scheduler `FUN_08012aaa` then runs canned script
  `0x11` or `0x12` (`FUN_08016710`):

  ```
  script 0x11 @0x0804c4b5:  M110 T1 / G1 X-66 F7000 / M110 T0 / G1 X66 F7000 / M111 T1
  script 0x12 @0x0804c4ea:  M110 T1 / G1 X-66 F7000 / M110 T0 / G1 X66 F7000 / M111 T2
  ```

  So in a file, **`M109 T1` = mirror, `M109 T2` = duplicate**, matching the
  community notes, but the match is byte-positional: it must be exactly
  `M109 T1` / `M109 T2` at the start of the line (single space, no other
  parameter before T).

### What M110 does

`M110 Tn` (case `0x6e`): waits idle, selects carriage n (`FUN_080113ec`),
clears dual mode (`0x20000008 = 0`). It is the raw carriage select the canned
scripts use. It does not move anything. (Not the Marlin line-number command.)

### What a host must send

Streaming over USB, to print duplicate:

```
~M110 T1\r\n
~G1 X-66 F7000\r\n
~M110 T0\r\n
~G1 X66 F7000\r\n
~M111 T2\r\n        (T1 for mirror)
```

Prerequisite: homed (G28) so the X offsets are meaningful. After this, the firmware adds 66 mm to every
G1 X target (`FUN_080106f4`), so the
G-code must be sliced for the half-bed coordinate system FlashPrint uses for
these modes (INFERRED from the offset; I did not verify slicer output).

Leaving dual mode: any `M108 Tn`/`M110 Tn` clears `0x20000008`; so do G28 (it
sets mode 1 with both carriages for homing, then `FUN_08011f34` clears the
flags when homing completes) and the start of an SD print (`FUN_08014768`).

For an uploaded file started with M23, `M109 T1/T2` lines in the file work as
described, provided the plugin does not rewrite them before upload.

Related, not IDEX modes: `M108 T0/T1` lines in an SD file, when the job header
`M118` declared two different tools, trigger tool-change scripts `0xF`/`0x10`
(park, `M906` current swap, unpark, `M6`). The exact script text for `0xF`/`0x10`
is built by `sprintf("%s%sM6\n", ...)` and my reading of its second argument is
uncertain; not needed for host streaming. `M118 ... D1/D2` starts the
"calPad" nozzle-offset calibration job (`FUN_0801252a`, strings `;calPad_A_start`
etc.), also unrelated to duplicate/mirror.

---

## Q3. Temperature waits

### M6 `FUN_08010ede` (CONFIRMED)

- Parameters: **P only** (timeout seconds, default 900). **S and T are not
  parsed.**
- Sets MoveMode WAIT_ON_TOOL (4) and timer `0x20000fc0`. `ok\n` is sent
  immediately; the *next* queued command waits.
- Exit (`FUN_08012aaa`, MoveMode 4 branch):
  - timeout, or
  - dual-tool job (`0x2000000e == 1`): neither extruder is "heating and not at
    target" (`FUN_08017d92` false for both), or
  - otherwise: the extruder selected by **`0x20000044`** has reached target
    within +/-2 C (`FUN_08017d4c`).
- `0x20000044` is written only by `M118 T<n>` (CONFIRMED by xref). It is 0 at
  boot, so M6 waits on **T0** unless an M118 said otherwise, and it stays at
  whatever the last SD job's M118 set.
- With no target set (target 0, nozzle warm) `FUN_08017d4c` never reports
  reached, so **M6 blocks for the full 900 s.**

### M7 `FUN_08010f16` (CONFIRMED)

- P only (timeout seconds, default 1800). MoveMode WAIT_ON_PLATFORM (5).
- Exit: timeout, bed target <= 0 (`FUN_08017d92` false), or bed within +/-2 C.

### M109 / M190

- M109: waits for idle motion, nothing else. Does not set a temperature, does
  not wait for one.
- M190: unknown M-code, immediate ok, nothing happens.
- The firmware never answers M109/M190 with temperature lines.

Correct host translation:
`M109 S<t> T<n>` -> `M104 S<t> T<n>` then wait host-side by polling M105
(or `M6` only when n matches `0x20000044`).
`M190 S<t>` -> `M140 S<t>` then `M7`.

---

## Q4. Tool change: M108 Tn vs bare Tn (CONFIRMED)

- **Bare `T0`/`T1`**: no G or M, so the executor prints
  `echo:Unknown command: "~T1"\r\n` and `ok\n`. No tool change.
- **`M108 Tn`** (case `0x6c`): waits idle; if `0x2000000e == 0` and T present,
  `0x20000043 = n`, clears dual mode, calls `FUN_080113ec(n)` which enables only
  that X carriage, sets the active/inactive carriage motor values (80/60 via
  `FUN_080171ac`) and switches the X position bookkeeping. It does **not** park
  or move.
- `0x2000000e` is written only by M118 (CONFIRMED by xref). After any SD job
  whose M118 named two different tools, **M108 from the host is ignored** until
  another M118 with matching T values arrives. Reboot also clears it.
- **`M110 Tn`** does the same as M108 without the `0x2000000e` check. It is the
  dependable host-side tool select.
- `M104 ... T1` and M105 already address tools by index; no M108 is needed for
  heating.

---

## Q5. SD / file operations

### Volumes (CONFIRMED)

FatFs with `_VOLUMES = 2` and no relative-path support:

- `disk_initialize` `FUN_08032944`: drive **0** = SPI NOR flash (JEDEC ID check
  `0xEF4018`, Winbond W25Q128); drive **1** = SD card (`FUN_0801a61c`).
- Drive 0 holds UI resources: `0:Font18.bin` (`0x0801bf9c`), `0:korean.xbf`,
  `0:yahei24.xbf` (`0x080378c0..`).
- Drive 1 holds user data: `1:/sys/...`, `1:log.txt`, and the UI's "copy model"
  writes to `1:/user/%s` (`FUN_08025ba6`, string `0x08025e74`).
- `find_volume` `FUN_08033e9c`: a path without `N:` uses **volume 0**. There is
  no current-drive variable.

So **`0:/user/<name>` points at the font flash**, not the SD card. The open
fails there (INFERRED: no `/user` directory on the font volume), producing
issue #78's `open failed, File: 0:/user/<name>.` A path without a prefix also
goes to volume 0. **Use `1:/user/<name>`.**

Paths are converted UTF-8 to UTF-16 into a 100-byte buffer (`FUN_080162b0`):
at most 49 characters including `1:/user/`, longer paths are **silently
truncated**. M28 and M23 truncate identically, so they stay consistent.

### M28 `<size> <path>` `FUN_080143ea` (CONFIRMED)

1. If MachineStatus is BUILDING_FROM_SD: `Busying.\r\nok\r\n`.
2. `disk_initialize(1)` fails: `Disk read error.\r\nok\r\n`.
3. size = `strtol(text after "M28 ")`; path = text after the first space
   following that (`strchr(p+5,' ')+1`).
4. Free space on `1:` (`f_getfree`, `FUN_08034cb2`); if
   `free_KB*1024 - 50 < size`: `out of space\r\nok\r\n`.
5. `f_open(path, FA_WRITE|FA_CREATE_ALWAYS)` (mode 10, `FUN_0803429c`).
   - fail: `open failed, File: <path>.\r\nok\r\n` (`0x080149dc`).
   - success: close, UI update, reopen, set receive mode `0x2000027a = 1`,
     respond `Writing to file: <path>  Length: <size>\r\nok\r\n` (two spaces;
     three if the two-space line would be exactly 64 bytes).

The drive prefix is whatever the host sends. Only the free-space check is tied
to `1:`.

### Data channel (CONFIRMED, `FUN_08029272`)

While `0x2000027a == 1`, every USB OUT packet is file data, with no `~`
filter, appended to a buffer at `0x20008364` and flushed with `f_write` each
time it passes 4,096 bytes. The declared size is decremented by each packet's
length; at <= 0 the tail is flushed and receive mode ends **automatically**
(the file is still open). `f_write` runs inside the USB interrupt.

- An `f_write` error sets `0x20000258` and sends `Error:error writing to file`
  (`0x08029460`, CRLF).
- A packet shorter than 7 bytes starting `~M29` ends receive mode early and is
  also fed to the command ring.
- Bytes are not echoed and there is no per-chunk ack.
- The plugin's assumption of a second bulk endpoint pair for data could not be
  checked: the descriptors are in compressed init data I did not decode. The
  callback treats all OUT data the same, so INFERRED: either endpoint works.

### M29 `FUN_080143b4` (CONFIRMED)

Clears receive mode, `f_close`, sends `Done saving file.\r\n` then `ok\r\n`.
Normal sequence after a complete upload:
`CMD M29 Received.\r\nDone saving file.\r\nok\r\n`.

### M23 `<path>` (CONFIRMED)

`FUN_08014868`: `disk_initialize(1)`; `f_open(path, FA_READ)`; on success
`File opened: <path> Size: <bytes>\r\n` and `File selected\r\nok\r\n`.
The path is everything after `M23 ` (exactly one space assumed).

Then the dispatcher **unconditionally** calls `FUN_08014768`, which starts the
print: MachineStatus = BUILDING_FROM_SD, clears dual mode, opens the reader
(`FUN_08015fc2`; `.gx` files have their header skipped, `.g`/`.gcode` read
raw, by extension). **M23 starts printing immediately; there is no separate
M24 start.** It does this even when the open failed. What the reader then does
with an unopened file I did not trace (INFERRED risk: garbage or an immediate
"finished").

### M24 / M25 / M26 (CONFIRMED)

- M24: `ok\r\n` first; if paused, requests resume. No effect when not paused.
- M25: `ok\r\n` first; pauses **only if MoveMode is MOVING** at that instant and
  no cal-pad job runs. Otherwise silently ignored.
- M26: cancels when not mid-transition, then `ok\r\n`. Cancel clears the
  command queue (`FUN_080103b0`), so queued host lines lose their ok. Heaters
  off and MachineStatus back to READY happen in the abort path of
  `FUN_08015358`.

### M27 (CONFIRMED in disassembly, `0x08014ef0`)

Always answers, printing or not:

- If `0x2000041a <= 300`: `SD printing byte 0/0\r\nok\r\n`.
- `.gx` file: `SD printing byte <pos - off>/<size - gx_header - off>\r\nok\r\n`.
- plain G-code: `SD printing byte <pos - off>/<size - off>\r\nok\r\n`.

`off` is `0x20000024` (0 except cal-pad jobs). `0x2000041a` is incremented by
the print-screen UI refresh `FUN_0801e068` only while `0x2000001f == 1`, which
only `M118` (no D) or a finished cal-pad sets. **A file without an M118 line
reports `0/0` for the whole print.** The firmware never says "Not SD printing"
or "Done printing file"; completion shows up as MachineStatus going back to
READY.

---

## Q6. M119 `FUN_0801490e` (CONFIRMED)

Three transfers after the echo:

```
Endstop: X-max: %d Y-max: %d Z-min: %d\r\n
MachineStatus: %s\r\n
MoveMode: %s\r\nok\r\n        or   MoveMode: PAUSED\r\nok\r\n
```

`PAUSED` is printed when `FUN_0801048a` reports a pause (state `0x2000004e`
non-zero without the resume bit). There is no `Status: S:.. L:.. J:.. F:..`
line on this firmware.

MachineStatus `0x20000204`, table at `0x0804bad3` (17-byte entries):

| # | Text | Set by |
|---|---|---|
| 0 | READY | boot, print finished (`FUN_0801562c`), abort path of `FUN_08015358`, `FUN_08022dd4` |
| 1 | BUILDING | no writer found |
| 2 | BUILDING_FROM_SD | M23 / UI print start (`FUN_08014768`); reverts to 0 when no file is open |
| 3 | ERROR | no writer found |
| 4 | CANCEL_BUILD | `FUN_0801416a`, only from state 1, so unreachable in practice |
| 5 | BUILDING_ONBOARD | canned script run from the UI (`FUN_0801554a`); back to 0 when the script ends |
| 6 | HEAT_SHUTDOWN | no writer found |

INFERRED from the pointer-literal search: **a host-streamed print stays
`READY`** the whole time. Host-driven activity is visible only in MoveMode.

MoveMode `0x20000089`, table at `0x0804bb4a`:

| # | Text | Entered by | Left when (`FUN_08012aaa`) |
|---|---|---|---|
| 0 | READY | | |
| 1 | MOVING | G0/G1, G162 and similar | planner flags `0x200006a8/9` clear |
| 2 | DELAY | G4 | timer `0x20000fa0` expires |
| 3 | HOMING | G28 | homing state machine `FUN_08011f34` done |
| 4 | WAIT_ON_TOOL | M6 | Q3 |
| 5 | WAIT_ON_PLATFORM | M7 | Q3 |
| 6 | WAIT_ON_BUTTON | M0/M1 | UI button or P timeout |
| (PAUSED) | display only | pause state | |

---

## Q8. M105, M115, LEDs

### M105 `FUN_08014bf8` (CONFIRMED)

Two-extruder machines (config byte `*(0x20000110+0x1C) == 2`, `FUN_080135fc`):
`T0:%d /%d T1:%d /%d B:%d /%d\r\nok\r\n` (`0x08015014`), otherwise
`T0:%d /%d B:%d /%d\r\nok\r\n`. Values are integers: current temperature
(`int16` at heater+0x24), then target (`FUN_08017c84`). Note the space before
each `/`.

### M115 (CONFIRMED, `FUN_08014c68` case `0x73`)

Seven transfers after the echo:

```
Machine Type: Flashforge Creator Pro 2\r\n
Machine Name: <user name from 0x200001ca>\r\n
Firmware: V1.8 20210221\r\n
SN: %x-%x-%x\r\n                   (three words at 0x20001008, INFERRED MCU UID)
X: %d  Y: %d  Z: %d\r\n            (build volume from config)
Tool Count: %d\r\n
ok\r\n
```

### LEDs / M650-M652

- M146: accepted no-op (Q1 table).
- M650, M651: immediate responses (Q1 table). Meaning INFERRED.
- M652: not implemented (queued, unknown, ok).

---

## Q9. What makes a host-streamed print fail or stall

1. **Queue depth 10 plus a 128-byte ring.** Ten un-oked queued lines fill the
   queue (`buffer full\r\n` is sent at the 10th, no ok), the reader stops, the
   ring fills, and further USB bytes are **dropped without notice**
   (`FUN_08014252`, `FUN_08029272`). Keep at most a few queued commands in
   flight; one is safest.
2. **Dispatcher commands wait behind a pending line.** If the queue is full,
   a keepalive M105/M119 sits in the ring behind the pending line and gets no
   answer until a slot frees.
3. **Inline `;` comments**: the command is dropped and no ok is sent.
4. **Lines over 95 bytes** (with `~`) are split into two commands.
5. **Any queued line during an SD print or upload is dropped without ok.**
6. **No ok ever**: `M504`, `M601 S1`, and anything sent while not in control.
7. **Long blocking**: M6 with an unset target (900 s), M7 (up to 1800 s),
   M0/M1 without P (forever until the button is pressed). Commands queued
   behind them get no ok until they finish.
8. **Waits idle**: G28, G93, M18, M108, M109, M110, M111, M906, M907 hold the
   queue until all prior motion completes. That is a pause in motion, not a
   stall.
9. **Silent control loss** after ~655k idle loop passes (Q7); every later
   command vanishes. Re-send `M601 S0` when responses stop.
10. **M26 flushes the queue**, so outstanding oks never arrive.
11. **`ok\n` vs `ok\r\n`**: queued commands end in LF only; immediate ones in
    CRLF. Parse both.
12. **USB IN transfers are not serialised** (INFERRED, see Transport): merged
    or cut responses are possible when the host polls slowly.

---

## Simulator spec

Model state:

```
channel        0|1          # 2 is transient; treat M601 S<nonzero> as "no reply, stay 0"
queue          list, max 10 # each entry: line text (with ~), echo already sent
pending_line   str|None     # set when queue was full
ring           bytes, cap 127
rx_continue    bool         # last OUT packet was 64 bytes
idle_count     int          # simulator: wall-clock seconds, default drop at 4 s (configurable)
receiving      None | {path, remaining, data}
sd_printing    bool         # 0x20000278
machine_status 'READY' ...
move_mode      'READY' ...
paused         bool
tool           0|1          # 0x20000043
dual_mode      0|1|2        # 0x20000008 && 0x20000013 (1 mirror, 2 duplicate)
dual_tool_job  bool         # 0x2000000e, set only by M118
m6_heater      0|1          # 0x20000044, set only by M118
temps          {T0:(cur,tgt), T1:(cur,tgt), B:(cur,tgt)}
files          dict on volume "1:" ; volume "0:" has no /user
```

### Inbound USB packet

```
on OUT packet p (len n <= 64):
    idle_count = 0
    if receiving:
        if n < 7 and p.startswith(b"~M29"):
            receiving = None ; push p into ring
        else:
            append p to file ; remaining -= n
            if remaining <= 0: receiving = None   # file stays open until M29
        return
    if p[0] != '~' and not rx_continue: drop p
    else: push bytes into ring until full, drop the rest
    rx_continue = (n == 64)
```

### Line handling

```
read bytes from ring while no pending line:
    '\r' or '\n' ends a line; ';' starts a comment (flag persists, see Transport)
    line cut at 95 bytes
for a complete line L:
    if channel == 1: send "CMD %s Received.\r\n" % L[1:5].split(' ')[0]
    if comment flag was set: send "MSG Received.\r\n" on the next empty line; do not execute
    if channel == 0: only M601 (and M615) act; nothing else is sent
    elif L has 'M' and its number is in the immediate table: answer now
    elif upload in progress or sd_printing: discard, no reply
    elif len(queue) == 10: hold as pending_line
    else: queue.append(L); if len(queue) == 10: send "buffer full\r\n"
```

### Request -> response pairs (after the CMD echo unless stated)

```
~M601 S0   (channel 0) -> "CMD M601 Received.\r\nControl Success.\r\nok\r\n"   (no separate echo)
~M601 S0   (channel 1) -> "Control Success.\r\nok\r\n"
~M601 S1   (channel 0) -> (nothing)
~M601 S1   (channel 1) -> "Control failed.\r\nok\r\n"
~M602                  -> "Control Release.\r\nok\r\n" ; channel = 0
~M105                  -> "T0:25 /0 T1:25 /0 B:24 /0\r\nok\r\n"
~M115                  -> "Machine Type: Flashforge Creator Pro 2\r\n" "Machine Name: <name>\r\n"
                          "Firmware: V1.8 20210221\r\n" "SN: <x>-<x>-<x>\r\n" "X: <x>  Y: <y>  Z: <z>\r\n"
                          "Tool Count: 2\r\n" "ok\r\n"     (build volume comes from config; capture it from a real unit)
~M119                  -> "Endstop: X-max: 0 Y-max: 0 Z-min: 0\r\n" "MachineStatus: READY\r\n" "MoveMode: READY\r\nok\r\n"
~M114                  -> "X:0 Y:0 Z:0 A:0 B:0\r\nok\r\n"
~M27                   -> "SD printing byte 0/0\r\nok\r\n" (see Q5 for the non-zero form)
~M28 1234 1:/user/a.gx -> "Writing to file: 1:/user/a.gx  Length: 1234\r\nok\r\n" ; receiving
~M28 1234 0:/user/a.gx -> "open failed, File: 0:/user/a.gx.\r\nok\r\n"
~M28 ... (sd printing) -> "Busying.\r\nok\r\n"
<raw bytes>            -> (nothing)
~M29                   -> "Done saving file.\r\n" "ok\r\n"
~M23 1:/user/a.gx      -> "File opened: 1:/user/a.gx Size: 1234\r\n" "File selected\r\nok\r\n" ; start print
~M23 <missing>         -> "File open failed.\r\nok\r\n" ; firmware still enters BUILDING_FROM_SD
~M24 / ~M25 / ~M26     -> "ok\r\n"
~M112                  -> "ok\r\n" ; queue cleared
~M600                  -> "succeed\r\nok\r\n"
~M502                  -> "FILAMENT: 0 \r\nok\r\n"
~M504                  -> (echo only, no ok)
~M650                  -> "X: 0 Y: 0\r\nok\r\n"
~M651                  -> "X:0 Y:0 Z:0\r\nok\r\n"
queued (G1, M104, M140, M6, M7, M106, M107, M108, M110, M111, M146, M17, M18, G28, G90, G91, G92, M109, M190, ...)
                       -> "ok\n" when executed (LF only)
~T1                    -> 'echo:Unknown command: "~T1"\r\n' then "ok\n" when executed
~G1 X1 ;c              -> "MSG Received.\r\n" (on the following \n), no ok
```

Executor timing in the simulator: run the head of the queue only when
move_mode is READY; M6/M7/G4/M0 set move_mode and send their own ok at once;
"waits idle" commands are not consumed until simulated motion is finished.

---

## Plugin implications

Each item names the plugin code that disagrees with the firmware.

1. **Wrong drive prefix (issue #78).** `__init__.py` sends
   `M28 %d 0:/user/%s` and `selectFile("0:/user/%s")`. `0:` is the SPI font
   flash; use `1:/user/%s` for both. Keep names short: the full path is cut at
   49 characters.
2. **IDEX modes cannot work when streaming (issue #83).** `rewrite_gcode`
   turns `M109` into `M6`, so `M109 T2` becomes `M6 T2`, a 900 s wait on T0
   with no mode change. Even unrewritten, M109 from USB is only a motion-idle
   wait. To stream duplicate/mirror the plugin must emit the park sequence and
   `M111 T2` (duplicate) / `M111 T1` (mirror), see Q2. For SD uploads, upload
   the file unmodified so the printer's own `M109 T1/T2` handling runs.
3. **M109/M190 translation drops the temperature.** `M6`/`M7` ignore S and T.
   Translate `M109 S<t> T<n>` to `M104 S<t> T<n>` plus a host-side wait (poll
   M105) and `M190 S<t>` to `M140 S<t>` plus `M7`. M6 waits on T0 (or the last
   M118's tool), never on the T in the command.
4. **Tool change.** `Tn -> M108 Tn` fails after any dual-tool SD job (sticky
   `0x2000000e`). `M110 Tn` is unconditional. Bare `Tn` is an unknown command.
   The plugin swallows OctoPrint's `M110` line-number resets, which is right,
   but it must generate `M110 Tn` itself rather than pass M110 through.
5. **SD-print whitelist.** During SD printing the plugin still forwards
   `M110`, `M117` and `M400`. M117 and M400 are queued commands, so the
   firmware drops them with **no ok** and OctoPrint stalls. M110 is dropped by
   `rewrite_gcode` anyway. Safe while SD printing: M24, M25, M26, M27, M105,
   M112, M114, M115, M119.
6. **M27 parsing** in `flashforge.py::_parse_response`: the firmware never
   sends "Not SD printing" / "Done printing file"; the plugin fabricates them
   from M119. That is the right approach, but expect `SD printing byte 0/0` for
   the entire print when the file has no M118 line; progress must then come
   from elsewhere.
7. **M23 starts the print**, as the plugin comment says. It also starts one
   when the open failed, so check for `File open failed.` and send M26.
8. **M146 does nothing** on this printer; filtering it is harmless.
9. **G91 is a no-op.** The FFCP2 profile needs the plugin's `noG91`
   conversion.
10. **Keepalive.** Any OUT packet resets the idle counter; the 0.5 s tick in
    `keep_alive()` is fine. But the drop is silent, and the plugin has no
    recovery: when responses stop, re-send `~M601 S0`.
11. **Upload pre-commands** `M104 S0 T0` etc. are queued: their reply is
    `CMD M104 Received.\r\nok\n`. `sendcommand` matches `\r\nok`, which works.
    They are dropped without ok if an SD print is running, which the
    `is_ready()` check is meant to prevent.
12. **Upload end.** After the last data byte receive mode ends by itself;
    `M29` then answers `CMD M29 Received.\r\nDone saving file.\r\nok\r\n`. The
    plugin's "CMD M28 in response, read again" path handles a late M28 reply
    and is harmless.
13. **Comments.** Never send inline comments; the command is dropped without
    ok. OctoPrint strips them by default; anything the plugin injects should
    not contain `;`.
14. **`ok` line endings.** Queued commands answer `ok\n`, immediate ones
    `ok\r\n`. `readraw()` uses `strip().endswith(b"ok")`, which tolerates both.

## Open questions (need hardware or more RE)

- Wall-clock length of the inactivity drop (Q7).
- USB descriptors / endpoint count (compressed init data not decoded).
- Whether back-to-back IN transfers actually corrupt responses.
- Text of tool-change scripts `0xF`/`0x10`.
- Behaviour after `M23` on a file that failed to open.
- Physical direction check for mirror vs duplicate (the code labels are
  consistent with the community notes, which is independent support).
