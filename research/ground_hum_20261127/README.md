# Residual ground hum after stage 05b (2026-11-27)

These scripts only read data. They measure narrow tonal lines in 30-second or
120-second windows spread across the whole tape.

- `survey.py`: prominence and width of each 60·k Hz harmonic in orig, 03, 04 and 05b
- `peaks.py`: persistent narrow lines between 40 and 2500 Hz, at any frequency
- `zoom.py`: the strongest lines near 60, 120, 180 and 300 Hz, at 0.05 Hz resolution

## Findings

1. Stage 03 has already removed the exact 60·k Hz comb. On T510087 CH27, the
   60 Hz line falls from about 68 dB above the local floor to the noise floor.
   The 180–1020 Hz odd harmonics fall from 40–65 dB to about 15–20 dB. On
   T510088 CH10, the 60·k lines are at the noise floor.
2. The hum that is left is the same in the 03, 04 and 05b outputs. Deflutter
   does not create it or change it.
3. Most of the residual is not on the 60 Hz grid:
   - T510087 CH27: sidebands at 60·k ± 6.5 Hz (53.5/66.5, 113.5/126.5, 173.5,
     306.5 Hz, ...), already present in the original. There are also windows
     (6.3 h and 14.7 h) where 180 and 300 Hz remain about 44 dB above the floor.
   - T510088 CH10: a drifting family, about 58.7 Hz falling to 58.4 Hz across
     the tape (117.4 and 176.1 Hz), plus lines at 52–54 Hz and 110–111 Hz.
     Playback-machine hum would sit at exactly 60.000 Hz. These lines drift, so
     they were recorded in 1971. A likely source is generator hum from a remote
     site on the network loop.
4. A fixed 60 Hz comb or notch would do almost nothing. Any reduction step has
   to find and track narrow lines at whatever frequency they occur.
