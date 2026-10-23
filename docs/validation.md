# Real-printer validation log

Measurements from the actual Creator Pro 2 (stock firmware V1.8 20210221) that the simulator and the
emulated firmware are checked against. Each entry says what was measured, how, and what the twin
predicted. A twin result that is not listed here is unvalidated.

## 2026-10-22: first session (OctoPi on a Pi 4, plugin 0.2.7 at commit 2e93cae, OctoPrint 1.11.8)

| What | Real printer | Twin prediction | Verdict |
|---|---|---|---|
| Idle M119 endstop line | `Endstop: X-max: 1 Y-max: 0 Z-min: 0` | emulated firmware: same (X reads 1 untriggered); hand model: all 0 | firmware twin right, hand model wrong |
| M105 format | `CMD M105 Received.` / `T0:a /b T1:c /d B:e /f` / `ok` | same | right |
| USB id | `2b71:000e` "FlashForge Creator Pro 2 3D Printer" | same | right |
| Cold bed reading, room 22.2 C (72 F, thermostat) | **16 C** | emulated firmware with the guessed thermistor: 13 C | direction right (reads low), magnitude 3 C off; bed thermistor/pull-up parameters need fitting |
| Bed heat-up 27 -> 49.5 C at target 50 | **91 s** | plant guess: ~3 min to 60 C | plant bed too slow; fit from `test_square_temps.csv` |
| 20 mm test square, right head, PLA 210/50, 3 layers | completed, 0 errors, 1142 commands, owner reports a perfect square | n/a | plugin path validated end to end |

Plugin behaviour confirmed on hardware in the same session:
- `M190 S50` went out as `M140 S50` + `M7`, `M109 S210 T0` as `M104 S210 T0` + `M6 T0` (targets kept).
- Temperature polls arrive as `ok T0:..`; zero "Externally triggered heatup detected" during the job.
- The printer's own preheat screen sets both heads to 240 C; OctoPrint never sent those targets.

Not yet validated: anything about motion dynamics (masses, belt stiffness, lost steps), the hotend
thermal model, SD upload on hardware, IDEX duplicate/mirror, tool changes.

Raw data (not committed, on the owner's Mac): `~/Documents/octopi-backup/test_square_temps.csv`
(2 s samples of all temperatures and targets through the job) and `test_square.gcode`.
