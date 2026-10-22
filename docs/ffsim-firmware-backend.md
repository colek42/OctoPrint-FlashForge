# ffsim firmware backend: the real stock firmware in an emulator

`ffsim.firmware` runs the vendor's FlashForge Creator Pro 2 firmware (V1.8 20210221) in Unicorn,
on a register-level STM32F4 model, wired to a physics model of the printer. It has the same
interface as the hand model `ffsim.FFCP2`, so `ffsim.install()`, the transport tests, the
`python -m ffsim` OctoPrint launcher and the 3D viewer all work against it.

The firmware image is not in this repository and must never be committed (`*.bin` is in
`.gitignore`). Neither may Ghidra projects or decompiled listings.

## Running it

```
export FFSIM_FIRMWARE=/path/to/creatorp2_v1.8_20210221.bin
python -m pytest tests/                      # hand model + firmware (transport tests run on both)
python -m pytest tests/test_firmware.py -v   # the acceptance tests; slow, see timings below
python -m ffsim --backend firmware           # OctoPrint + 3D viewer on the real firmware
python -m ffsim --backend firmware --plant my-printer.json   # measured parameters
```

From Python:

```python
from ffsim.firmware.backend import FirmwareFFCP2
sim = FirmwareFFCP2()            # boots the firmware: ~5 simulated seconds
sim.bulk_write(0x01, b"~M601 S0\r\n")
sim.bulk_read(0x81, 512, 100)    # timeout is SIMULATED milliseconds
sim.run_for(30.0)                # advance 30 simulated seconds
sim.plant.axes["x0"].lost_steps, sim.following_error_max, sim.ram(0x20000013)
```

Simulated time only advances while somebody waits on it (`bulk_read`, `run_for`). Tests are
therefore deterministic and independent of host load. `realtime=True` (used by
`python -m ffsim`) runs a background thread that keeps simulated time in step with the wall
clock, falling behind when the emulator cannot keep up.

## Layers

| Layer | Files | Knows about |
|---|---|---|
| (a) CPU, memory map, clock, exceptions | `cpu.py`, `native.py` | Cortex-M4 only |
| (b) peripherals and devices | `scs.py`, `periph.py`, `soc.py`, `devices.py` | STM32F4 registers (RM0090), ADS1118 datasheet |
| board wiring | `board.py` | which pin goes to which motor, heater, switch, sensor |
| (c) physics plant | `plant.py` | masses, belts, motors, heaters; sees only pins and PWM duty |
| (d) firmware adapter | `stock.py` | every stock-firmware address, and nothing else does |
| backend | `backend.py` | the `ffsim.FFCP2` interface, threading, the viewer state |

Nothing in (b), (c) or `board.py` references a stock-firmware address. A Klipper adapter would
replace only `stock.py` (see "Klipper" below).

## What is emulated and what is HLE'd

Emulated at register level (the firmware's own code runs against these):

- **CPU**: Thumb-2 + FPU (Unicorn 2.1.4, Cortex-M4 model). Exceptions are stacked in Python
  (8-word frame, FP caller-saved registers saved off-stack), with priorities, PRIMASK/BASEPRI,
  tail-chaining and nesting.
- **NVIC, SCB, SysTick, DWT CYCCNT**: SysTick fires on the cycle it is due. CYCCNT is the cycle clock.
- **RCC/PWR/FLASH**: everything reports ready.
- **GPIO A-I**: outputs notify the board on every edge, inputs come from the plant.
- **TIM1-14**: counter derived from the clock, update and compare interrupts, PWM duty readout
  (the hotend heaters are TIM5 CH4/CH3 PWM on PA3/PA2).
- **ADC1 + DMA2 stream 0**: the bed NTC. Continuous conversion refreshes the DMA buffer every
  1 ms instead of every 2 us (see gaps).
- **SPI1-3**: the ADS1118 thermocouple ADC sits on SPI3 with CS on PF12, modelled from the
  datasheet: config word in, previous conversion out, internal temperature sensor as the cold
  junction, PGA, MUX.
- **USART, OTG_FS, FSMC (LCD), backup SRAM**: register files or plain memory, enough for the
  firmware to run. The display output is ignored.

High-level emulation, all in `stock.py`:

- **USB**: host packets (max 64 bytes) are delivered by calling the firmware's DataOut callback
  `FUN_08029272(buf, len)` as a pseudo-interrupt at the OTG IRQ priority. Replies are captured
  at `DCD_EP_Tx` (`FUN_0802e176`). The OTG_FS register block is not modelled.
- **Storage**: FatFs `disk_initialize/disk_read/disk_write` (`FUN_08032944/080329ba/08032ab6`)
  are served from in-memory FAT16 images: drive `0:` is the 12 MiB resource area of the W25Q128
  SPI flash (empty here: no fonts), drive `1:` a 32 MiB SD card with `/user` and `/sys`. The
  firmware's own FatFs writes the files, and `sim.files` reads them back.

## The physics plant

All parameters are in `PlantParams` (`plant.py`). Each one carries its source or a reasoned
estimate marked GUESS. Override any subset from JSON or YAML (`PlantParams.load`, `--plant`,
or `FFSIM_PLANT=...`). Unknown keys are rejected, so a typo cannot silently keep a guess.

**Thermal** follows the Marlin/Kalico MPC model (Kalico `klippy/extras/control_mpc.py`) and
uses its parameter names, so an `MPC_CALIBRATE` result on the real printer drops straight in:

```
C dTb/dt = P*duty - (Tb - Ta)*h(fan) - (Tb - Ta)*v_e*A_f*rho_f*c_f
dTs/dt   = (Tb - Ts) * sensor_responsiveness
```

| Parameter | Hotend (T0, T1) | Bed | Source |
|---|---|---|---|
| heater_power | 40 W | 150 W | brief / GUESS |
| block_heat_capacity | 14.5 J/K | 600 J/K | GUESS, chosen so 25 to 210 C takes ~90 s at full power / bed 60 C in ~3 min |
| ambient_transfer | 0.10 W/K | 1.5 W/K | GUESS |
| fan_ambient_transfer | 0.10, 0.13, 0.15 W/K | - | GUESS, Kalico example shape |
| sensor_responsiveness | 0.0999 /s | 0.2 /s | Kalico MPC.md example / GUESS |

The sensors are fed back through what the firmware actually reads:

- **Thermocouples**: type K EMF (NIST ITS-90 polynomials, copied from Klipper PR #6553
  `ads1118.py`, GPLv3) of the sensor temperature minus the EMF of the cold junction (board
  temperature = ambient + 5 C, GUESS). The ADS1118 converts that to a code at the PGA the
  firmware configured.
- **Bed NTC**: EPCOS 100K B57560G104F Steinhart-Hart (Klipper's calibration points) through a
  4.7 k pull-up to 3.3 V, then a 12-bit code.

**Motion**: one model per stepper, `x0` (right carriage, Klipper `stepper_x`), `x1` (left,
`dual_carriage`), `y`, `z`, `e0`, `e1`.

```
J  th'' = T_avail(w) * sin(Np (th_cmd - th)) - c_r*w - r*F       Np = 50 (200-step motor)
m  x''  = F - friction - m*g (Z)
F       = k (r th - x) + c (r th' - x'),     k = EA / free belt length
T_avail = kt * min(I_rated, (V - kt*w) / |R + j*Np*w*L|)
```

th_cmd comes from STEP edges on the real pins (honouring DIR, ENABLE and their inversions).
When the load angle passes +-pi the rotor drops into the next stable equilibrium: that is 4
full steps lost. It is counted per axis (`lost_steps`, in microsteps), and the true carriage
position stays off from what the firmware believes. `following_error_max` is the largest
dynamic lag (mm), with lost steps excluded. Z is a self-locking lead screw with gravity. The
extruders are a light geared load.

| Parameter | Value | Source |
|---|---|---|
| X carriage mass | 0.52 kg each | GUESS 0.45-0.6 (motor ~0.28 kg + hotend + fans + carriage) |
| Y moving mass | 1.75 kg | GUESS 1.5-2 (X rail + both carriages) |
| Z moving mass | 1.2 kg | GUESS (plate + glass + carriage) |
| rotation_distance, microsteps | X/Y 34 mm, Z 8 mm, E 32 mm; 16 | Klipper PR #6553 |
| belt EA, free length, damping | 1e5 N, 0.4 m (Y 0.3 m), zeta 0.05 | GUESS |
| motor | 0.40 N*m, 1.5 A, 1.5 ohm, 2.8 mH, 54 g*cm^2, 24 V | GUESS (17HS4401-class NEMA17; model unidentified) |
| rotor damping ratio | 0.1 | GUESS |
| travel and endstops | x0 -38..236 (switch at 236), x1 -38..198 (switch at -38), y -3..152 (switch at -3), z -4..172 (switch at -4) | Klipper PR #6553 |

What the model predicts with these guesses: a smooth step train holds at 300 mm/s and the X
motor stalls between 300 and 450 mm/s (accelerations of 3,000 to 30,000 mm/s^2 make little
difference). The stock firmware caps X at ~286 mm/s (26,900 steps/s) whatever F says, so stock
G-code cannot make this printer skip on X in the model. The stall edge moves with the motor
guesses (holding torque, inductance, supply voltage), so measure those before trusting it.

Both carriages and the gantry emit viewer segments from their physical (ringing) positions.
In duplicate or mirror mode the second carriage draws its own part. `plant.start_accel_capture()`
and `stop_accel_capture(path)` write the carriage acceleration at 3200 Hz in the ADXL345 CSV
format (`#time,accel_x,accel_y,accel_z`, mm/s^2) that Klipper's `calibrate_shaper.py` reads.

Calibration path: weigh a head and replace `axes.x0.moving_mass`. Run `MPC_CALIBRATE` under
Kalico and paste `block_heat_capacity`, `sensor_responsiveness`, `ambient_transfer` and
`fan_ambient_transfer`. Fit belt EA and damping to an ADXL345 resonance capture.

### Y moves the X gantry: how I know, and how sure I am

The Klipper config uses `kinematics: cartesian` with a single Y stepper and a Z bed that has
leveling screws (`[bed_screws]`). BrennerLu's pinout has exactly one Y driver. The printer
descends from the MakerBot Replicator 2X layout, where both X carriages ride one rail that
Y carries, and the bed only moves in Z. Nothing in the firmware tells the two apart. This is
INFERRED from the lineage and the configs, not from a teardown photo.

## Pins and where the sources disagree

Steps/dir/enable inversions, endstop polarity, heater pins, ADS1118 CS and the 16x microstepping
come from the Klipper config (PR #6553). BrennerLu's CubeMX export supplied the labels. Notes
from comparing them with each other and with what the stock firmware does in the emulator:

- **ADS1118 is on hardware SPI3, not bit-banged.** BrennerLu labels PC10 "GPIO_Output" (SCLK).
  The stock firmware puts PC10/PC11/PC12 in alternate-function mode (AF6, SPI3) and drives CS
  on PF12, which agrees with Klipper's `spi_bus: spi3a` / `cs_pin: PF12`. BrennerLu does not
  list PF12 at all.
- **Hotend heaters are TIM5 PWM** (PA3 = TIM5_CH4, PA2 = TIM5_CH3, ~976 Hz, ARR 255) in the
  stock firmware, not plain GPIO as the CubeMX labels suggest. The bed (PA1) is a GPIO output.
- **T0 is the right head.** The stock firmware's heater T0 uses ADS1118 MUX 0 (config 0x0C62)
  and drives PA3. Klipper's `[extruder]` (right) uses `ads1118_mux: 0` and `heater_pin: PA3`.
- **Endstops.** The stock homing sequence drives the right carriage toward Klipper's +X (switch
  PC6, at X max), the left one toward -X (PC7, at X min), Y toward Klipper's -Y (PG0) and Z
  toward Klipper's -Z (PG1). It stops exactly where those switches trigger with Klipper's
  polarity (PC6/PC7 active high, PG0/PG1 active low). That resolves the M119 wording: "X-max"
  is the right carriage's switch at the max end. "Y-max" is the switch Klipper and BrennerLu
  call Y min; the stock firmware homes toward it and reports Y = -78 there (its own frame is
  centred). M119 also prints the polarity inconsistently: `X-max: 1` means *not* triggered,
  `Y-max: 1` means triggered (observed in emulation with Klipper's pin polarities).
- **The bed thermistor curve.** Klipper's config says EPCOS 100K B57560G104F. The stock firmware's
  20-point table (`0x0804c5b4`) fits a 100 k, beta 3955 NTC with a 4.8 k pull-up to within
  0.4 C. The plant uses EPCOS + 4.7 k. Both are 100 k at 25 C, so room-temperature readings
  agree; they diverge above ~80 C (beta 3950 reads up to ~8 C higher at 150 C).
- **Steps/mm.** The stock firmware's axis table: X/Y 94.1397, Z 400, E 96.2752.
  Klipper: X/Y 3200/34 = 94.1176 (0.02% apart), Z 400, E 100 (rotation_distance 32).
  The extruder disagrees by 3.9%.
- **The "VS??" pins.** Two per driver. The stock firmware drives all twelve high and never
  changes them. Klipper never configures them, yet runs at `microsteps: 16`. The consistent
  reading is that they are microstep-select inputs with "both high" (or board pull-ups) giving
  1/16. They are not current control: M906 sets the X-carriage currents through an I2C digipot
  at address 0x2F (MCP4017/18/19 family) on bit-banged pins (`FUN_080171ac` -> `FUN_080173f4`).
  The model therefore keeps 16 microsteps fixed and ignores M906. INFERRED.
- **MCU.** BrennerLu names an STM32F417ZGTx, the task brief an F407. The firmware uses nothing the
  F417 adds (crypto/hash), so the F407 memory map serves both.

## Speed

Measured on a 12-core M-series Mac under heavy unrelated load (load average ~45), so treat the
numbers as lower bounds:

| Workload | Simulated s per wall s |
|---|---|
| boot (5 s of mostly delay loops) | ~0.7-1.0 |
| idle, host polling M105/M119 | ~0.5-0.8 |
| heating, PID active | 0.25-0.8 (falls as the heater PWM toggles more) |
| long G1 at 50 mm/s (x0 plant integrating at 50 kHz) | ~0.55 |
| G1 at the firmware's 286 mm/s cap | ~0.5 |

The default `cpi=4` charges four cycles per instruction: the CPU is under-clocked to ~42 MIPS
to save emulation work. ISRs still fit (SysTick and TIM14 deliver at exactly 10 kHz). Main-loop
counts stretch, though, and the stock USB idle drop is counted in main-loop passes: at cpi=4 it
comes after roughly 4x the real wall time. Use `cpi=1.0-1.3` for timing studies of that drop.

Where the time goes: Unicorn itself (native, ~65 M instructions/s, bounded by a C block hook in
`native.py` rather than Unicorn's slow per-instruction `count=`), plus ~20,000 Python-delivered
interrupts per simulated second and the Python motion integrator while axes move. The next
real speedup is the expensive one: move NVIC/SysTick/timer delivery and the axis integrator into
the C helper. That is also what Klipper needs (see below).

## Known gaps

- **IPSR reads 0 in handlers** (the core stays in thread mode). The stock firmware never reads
  it. Klipper's `irq_save()` uses PRIMASK, so this is fine there too, but it would break an RTOS.
- **Interrupt latency** is one basic block plus, after `CPSIE`, up to one 20 us chunk:
  unmasking does not stop the emulator. Pending exceptions are taken at exception return, at
  NVIC writes and at chunk boundaries.
- **Cycle accounting** charges `cpi` cycles per 2.6 bytes of executed code (GUESS for Thumb-2
  density). It is not per-instruction timing, and there are no flash wait states.
- **Poll skipping**: a loop that reads one register with no MMIO write in between is
  fast-forwarded to the next event (at most 100 us), only while interrupts are enabled and
  nothing is pending. Busy-wait delays can end up to 100 us late.
- **ADC continuous mode** refreshes the DMA buffer every 1 ms, and ADS1118 conversions are
  instantaneous: no data-rate delay, no noise.
- **No I2C EEPROM model.** The stock firmware keeps machine settings in a 24Cxx on bit-banged
  PG3 (SCL) / PG4 (SDA) at address 0xA0 (`FUN_0801b306`). With no device answering, every read
  returns 0xFF: `M115` prints a garbage machine name and `SN: 0-0-0`, and the X frame after G28 is
  the erased-EEPROM default (X = 0 at the right endstop). A real EEPROM dump would fix both.
- **No touchscreen input, no display output, empty resource flash** (no fonts). The UI runs
  but draws nothing anyone sees.
- **Carriage-carriage collisions** are not modelled. The two X carriages can pass through each other.
- **Fans** feed the MPC fan term from the part-fan pin duty only; heatbreak fans are not modelled.
- **Filament** flow into the hotend (the MPC filament term) is wired to zero.

## Where the real firmware differs from the hand model (`ffsim/__init__.py`)

Observed by sending the same commands to both (`docs/ffcp2-firmware-protocol.md` gives the
addresses):

1. Queued commands answer `ok\n` (LF only). The hand model sends `ok\r\n` for everything.
2. `M6`/`M7` answer `ok` at once and then block the *queue*, not the channel. Later queued
   commands get no ok until the wait ends; immediate ones (M105, M119, M114) still answer. M6
   ignores S and T and waits on T0 for up to 900 s. The hand model blocks the whole channel
   and backlogs everything.
3. `M105` at 25 C ambient reads `T0:24 T1:24 B:19`: the stock bed table's last segment spans
   29 C to -29 C over 215 counts, so a 25 C bed reads ~19 C. The hand model says 25.
4. `M119` at rest: `Endstop: X-max: 1 Y-max: 0 Z-min: 0` (hand: `0 0 1`).
5. `M115`: `X: 200  Y: 148  Z: 150` with two spaces, `SN: 0-0-0`, and an EEPROM-derived name (hand:
   one space, `SN: SIM-0000`, `FFCP2-SIM`).
6. `T1` gives `echo:Unknown command: "~T1"` then `ok\n`. The hand model acknowledges it silently.
7. `G91` is a no-op that still answers ok. The hand model has no G91.
8. A line with an inline comment (`G1 X10 ;c`) is echoed, then `MSG Received.`, never executed,
   and gets no ok. The hand model executes it.
9. `M146` is a no-op. The hand model stores the colour.
10. `M29` without an open upload still says `Done saving file.` (hand: `File error`).
11. `M23` on a missing file says `File open failed.`, then starts a print anyway and floods
    `echo:Unknown command: ""` until reset. Afterwards `M28` answers `Busying.`
12. `M504` answers nothing at all, not even ok.
13. `M28 <n> /user/x.gx` (no drive prefix) resolves to drive 0 in FatFs `find_volume`
    (`FUN_08033e9c`, read in the decompile; in the differential run it answered `Busying.`
    because of item 11). The hand model rejects anything that is not `1:/user/`.
14. Host control lapses silently after an idle period counted in main-loop passes. The hand
    model's `control_timeout` defaults to never.
15. G28 answers ok immediately and homes in the background (~15 s). The hand model teleports to 0.
16. Upload data is counted per USB packet on the command endpoint. The hand model takes it only
    on the second endpoint pair.

## Klipper: what is left to boot it

The split above was built for this. A `klipper.py` adapter plus a board config is the plan,
not a rewrite:

1. **Loader**: read the Klipper ELF (`out/klipper.elf`, symbols). Load it at 0x08000000 (no
   bootloader, per PR #6553) and reset through its vector table.
2. **USB**: two options. (a) Implement the OTG_FS device-mode registers (core reset, FIFOs,
   IN/OUT endpoint control, the interrupts `usbotg.c` uses), about 400 lines, and drive
   enumeration from the host side. (b) HLE by symbol: hook `usb_read_bulk_out` and
   `usb_send_bulk_in` and call `usb_notify_bulk_in/out` through `inject_call`, then force the
   configured state. (b) is quicker; (a) also tests Klipper's USB stack. Either way the bytes
   go to a pty (`os.openpty()`) so an unmodified `klippy` connects to `/dev/ttys*`.
3. **Timing**: Klipper on the F4 uses `armcm_timer.c`, with DWT CYCCNT as the clock and SysTick
   compare for timers. Both are cycle-based here already, so timer interrupts fire on the
   correct simulated cycle (within one basic block). Run it with `cpi=1.0`, since the host's
   clock sync would see an under-clocked MCU.
4. **Real-time speed is the blocker.** klippy assumes the MCU keeps up with the wall clock and
   shuts down on "timer too close". The emulator currently runs at 0.5-0.8x. Klipper's step
   rate (tens of kHz of timer interrupts while printing) needs the interrupt delivery and the
   axis integrator in C, or a klippy patched to scale time. Expect 2-5x headroom from the
   native path. This is the job to do before Klipper.
5. **ADS1118**: Klipper's `ads1118.c` from PR #6553 drives the same SPI3/PF12 device model.
   Nothing to add.
