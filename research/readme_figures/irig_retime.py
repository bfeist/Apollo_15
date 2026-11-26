"""IRIG-B retiming evidence for the README (section 10): NARA flight tape and 30-track CH30.

Stage 07i section (``stage07i``): for each time-code tape, the newest complete 07i plan is read
with its 05b time-code decode, and the frame residual to the plan (GET of the frame minus
output + time-code-track constant) and the plan's speed are kept. The 07b check output of
T510075 CH30 (``irig/A15_T510075/check/CH30/retimed.wav``) is decoded once more here,
independently of 07i (cache ``t510075_ch30_retimed_frames.json``, ~2 min).

Read-only on the archive. Writes ``irig_retime.json`` beside this script.

* NARA 640-AAA tr4: strict frames of the original MP3 (saved survey) against the frames
  decoded again from the respeeded ``640-AAA_tr4.wav`` (decoded here once and cached in
  ``nara_640_tr4_respeed_frames.json``, ~5 min). Before: GMT - tape seconds. After: decoded
  label - (output seconds + segment constant).
* 30-track tapes with time code on delivered CH30 (stage-10 ``irig_nara`` decode of the
  originals): the unchanged ``nara_tape_map`` chain/segment/spline steps are run on the CH30
  frames. This is the projected correction; no 30-track audio has been retimed from IRIG.

    uv run python research/readme_figures/irig_retime.py
"""
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
NARA_CODE = REPO/'scripts/sg-audio/01_sg-nara'
sys.path.insert(0, str(NARA_CODE))
import nara_irig  # noqa: E402
import nara_tape_map as ntm  # noqa: E402

HERE = Path(__file__).resolve().parent
NARA = Path('F:/Apollo_15_data/processing/sg_nara')
CID = Path('D:/Apollo_15_30-Track/channel_id/irig_nara')
INV = json.loads((HERE/'inventory.json').read_text(encoding='utf-8'))
TAPE_NARA = '255-PAOa-640-AAA'
# The NARA bound (-0.5..+1 % about the tape's mean) is too tight for the 30-track, whose speed
# moves by up to 2.5 % within an hour (docs/20 finding 4); stage 07a's hard bound is 0.85-1.10.
THIRTY_RATE_BOUNDS = (-0.05, 0.05)
TAPES_30 = ('A15_T510075', 'A15_T510076', 'A15_T510077', 'A15_T510078', 'A15_T510085')


def gmt(day, h, m, s):
    return (day - 1)*86400 + h*3600 + m*60 + s


def nara():
    survey = json.loads((NARA/'irig'/TAPE_NARA/'tr4.json').read_text(encoding='utf-8'))
    align = json.loads((NARA/'align'/f'{TAPE_NARA}.json').read_text(encoding='utf-8'))
    respeed = json.loads((NARA/'respeed'/TAPE_NARA/'640-AAA.json').read_text(encoding='utf-8'))
    f = np.array(survey['frames'], float)
    f = f[f[:, 7] == 1]
    t = f[:, 0] + align['files']['tr4']['clock_start_s']
    o = gmt(f[:, 1], f[:, 2], f[:, 3], f[:, 4]) - t
    runs = ntm.chains(t, o)                       # same misread rejection as the map
    keep = np.concatenate(ntm.segments(t, o, runs))
    t, o = t[keep], o[keep]
    cache = HERE/'nara_640_tr4_respeed_frames.json'
    wav = NARA/'respeed'/TAPE_NARA/'640-AAA_tr4.wav'
    if not cache.is_file():
        frames, total = nara_irig.decode_stream(str(wav), None, 44100)
        cache.write_text(json.dumps([[fr.t, fr.clock] for fr in frames if fr.strict]), encoding='utf-8')
    a = np.array(json.loads(cache.read_text(encoding='utf-8')), float)
    seg = respeed['segments'][0]
    u, g = a[:, 0], a[:, 1]
    err = g - (u + seg['get_equals_output_plus_s'] + ntm.RANGE_ZERO_S)
    day_misread = np.abs(err) >= 43200                 # the SBS check does not cover the day field
    err = (err + 43200) % 86400 - 43200
    good = np.abs(err) < 0.5                          # any label still whole seconds off
    z = np.load(NARA/'maps'/f'{TAPE_NARA}.npz')
    return dict(tape=TAPE_NARA, track='tr4', before_t=t.tolist(), before_o=(o - o[0]).tolist(),
                after_u=u[good].tolist(), after_err_ms=(1000*err[good]).tolist(),
                after_misread=int((~good).sum()), after_day_misread=int(day_misread.sum()), map_t=z['t'][::50].tolist(), map_rate_ppm=(1e6*z['rate'][::50]).tolist(),
                added_s=respeed['added_s'], rate_ppm=[respeed['rate_ppm_min'], respeed['rate_ppm_max']],
                residual_rms_ms=seg['residual_rms_ms'], get_first=seg['get_first'], get_last=seg['get_last'])


def thirty(tape):
    rec = json.loads((CID/tape/'CH30.json').read_text(encoding='utf-8'))
    inv = next(r for r in INV['tapes'] if r['tape'] == tape)
    f = np.array(rec['frames'], float)
    t, g = f[:, 0], f[:, 1]
    # The 30-track plays 1.4-2.3 % slow, outside the NARA chain limits (0.5 %). Rescale file
    # time by the decode's median rate first; the NARA steps then run unchanged on that axis.
    r0 = rec['file_s_per_irig_s']
    ntm.RATE_BOUNDS = THIRTY_RATE_BOUNDS
    tau = t / r0
    o = g - tau
    runs = ntm.chains(tau, o)
    segs = ntm.segments(tau, o, runs)
    s = max(segs, key=len)
    taus, os_ = tau[s], o[s]
    bt, fit, res = ntm.fit_segment(taus, os_)
    ts = t[s]
    probe = np.arange(taus[0], taus[-1], 60.)
    rate = r0/(1 + fit(probe, 1))                      # file seconds per IRIG second
    filename_clock = inv['start'] - 86400            # filename start, seconds since 1971-01-01 (day 1)
    before = g[s] - (filename_clock + ts)            # GMT minus the filename clock at the same sample
    hum_rate = 60/inv['hum_hz']
    span_rate = 1/float(np.polyfit(ts, g[s], 1)[0])
    return dict(tape=tape, head=inv['head'], strict=rec['strict'], decoded_s=rec['decoded_s'],
                frames_used=int(len(s)), frames_rejected=int(len(t) - len(s)), segments=len(segs),
                t=ts[::20].tolist(), before_s=before[::20].tolist(), res_ms=(1000*res[::20]).tolist(),
                residual_rms_ms=float(1000*np.sqrt(np.mean(res[np.abs(res) < 0.05]**2))),
                residual_p99_ms=float(1000*np.percentile(np.abs(res), 99)),
                rate_t=(probe*r0).tolist(), rate=rate.tolist(), mean_rate=span_rate, decode_rate=r0,
                hum_rate=hum_rate, hum_hz=inv['hum_hz'],
                filename_error_start_s=float(before[0]), filename_error_end_s=float(before[-1]),
                smoothing=fit.smoothing)


RETIMING = Path('D:/Apollo_15_30-Track/stage_evidence/retiming/irig')


def latest_plan(tape):
    fits = [p for p in (RETIMING/tape).glob('fit-*') if (p/'plan.json').is_file() and (p/'manifest.json').is_file()
            and json.loads((p/'manifest.json').read_text(encoding='utf-8')).get('status') == 'complete']
    return max(fits, key=lambda p: (p/'plan.json').stat().st_mtime_ns)


def fold(err):
    """Misread day fields (not covered by the SBS check) are whole days off."""
    return (err + 43200) % 86400 - 43200


def stage07i(tape):
    fit = latest_plan(tape)
    plan = json.loads((fit/'plan.json').read_text(encoding='utf-8'))
    z = np.load(fit/'timing_map.npz')
    seg = plan['get_placement']['segments'][0]
    c = seg['timecode_track_get_equals_output_plus_s']
    dec = json.loads((RETIMING/tape/'decode'/'CH30.json').read_text(encoding='utf-8'))
    f = np.array(dec['frames'], float)
    t, g = f[:, 0], f[:, 1] - ntm.RANGE_ZERO_S
    err = fold(g - (np.interp(t, z['input_seconds'], z['output_seconds']) + c))
    good = np.abs(err) < 0.05
    k = slice(None, None, 600)  # 60 s of the 0.1 s grid
    out = dict(fit=fit.name, method=plan['method'], strict=int(len(t)), t=t[good][::10].tolist(),
               res_ms=(1000*err[good][::10]).tolist(), rms_ms=float(1000*np.sqrt(np.mean(err[good]**2))),
               p99_ms=float(1000*np.percentile(np.abs(err[good]), 99)), outliers=int((~good).sum()),
               speed_t=z['input_seconds'][k].tolist(), slow_pct=(100*(1/z['recorded_speed_ratio'][k] - 1)).tolist(),
               timecode_share=plan['support']['timecode_share'], sources=plan['support']['sources'],
               reference=plan['get_placement']['reference'],
               voice_offset_s=plan['get_placement'].get('timecode_track_offset_from_reference_s'),
               plan_rms_ms=seg['frame_residual_rms_ms'], plan_p99_ms=seg['frame_residual_p99_ms'],
               flags=plan['review_flags']['timecode'])
    if tape == 'A15_T510075':
        check = RETIMING/tape/'check'/'CH30'
        rj = json.loads((check/'retime.json').read_text(encoding='utf-8'))
        assert rj['shared_plan']['path'].endswith(str(Path(fit.name)/'plan.json')), 'check used another plan'
        cache = HERE/'t510075_ch30_retimed_frames.json'
        if not cache.is_file():
            frames, _ = nara_irig.decode_stream(str(check/'retimed.wav'), None, 8000)
            cache.write_text(json.dumps([[fr.t, fr.clock] for fr in frames if fr.strict]), encoding='utf-8')
        a = np.array(json.loads(cache.read_text(encoding='utf-8')), float)
        u = a[:, 0] + rj['shared_plan']['output_start_seconds']
        e = fold(a[:, 1] - ntm.RANGE_ZERO_S - (u + c))
        ok = np.abs(e) < 0.05
        hours = {}
        for h in range(int(u.max()//3600) + 1):
            m = ok & (u >= 3600*h) & (u < 3600*(h+1))
            if m.sum() > 100:
                hours[h] = float(1e6*np.polyfit(u[m], e[m], 1)[0])
        out['check'] = dict(frames=int(len(a)), used=int(ok.sum()), median_ms=float(1000*np.median(e[ok])),
                            rms_ms=float(1000*np.sqrt(np.mean(e[ok]**2))), p99_ms=float(1000*np.percentile(abs(e[ok]), 99)),
                            hourly_rate_ppm=hours, u=u[ok][::10].tolist(), res_ms=(1000*e[ok][::10]).tolist())
    return out


if __name__ == '__main__':
    out = dict(nara=nara(), thirty={t: thirty(t) for t in TAPES_30}, stage07i={t: stage07i(t) for t in TAPES_30})
    for t, r in out['stage07i'].items():
        print(f"07i {t} {r['fit']} {r['method']}: {r['strict']} frames, rms {r['rms_ms']:.2f} ms p99 {r['p99_ms']:.2f} "
              f"(plan {r['plan_rms_ms']:.2f}/{r['plan_p99_ms']:.2f}), outliers {r['outliers']}, tc {r['timecode_share']:.1%}, "
              f"slow {min(r['slow_pct']):.2f}..{max(r['slow_pct']):.2f} %, voice offset {r['voice_offset_s']}")
    ch = out['stage07i']['A15_T510075']['check']
    print(f"check T510075 CH30 retimed: {ch['used']}/{ch['frames']} frames, median {ch['median_ms']:+.3f} ms, rms {ch['rms_ms']:.3f}, "
          f"p99 {ch['p99_ms']:.3f}, hourly rate ppm {min(ch['hourly_rate_ppm'].values()):+.2f}..{max(ch['hourly_rate_ppm'].values()):+.2f}")
    (HERE/'irig_retime.json').write_text(json.dumps(out), encoding='utf-8')
    for t, r in out['thirty'].items():
        print(f"{t} {r['head']}: {r['frames_used']}/{r['strict']} frames, {r['segments']} segment(s), "
              f"IRIG rate {r['mean_rate']:.5f} vs hum {r['hum_rate']:.5f} ({(r['mean_rate']/r['hum_rate']-1)*100:+.3f} %), "
              f"filename error {r['filename_error_start_s']:+.1f} -> {r['filename_error_end_s']:+.1f} s, "
              f"fit residual rms {r['residual_rms_ms']:.2f} ms p99 {r['residual_p99_ms']:.2f} ms, "
              f"rate {min(r['rate']):.4f}..{max(r['rate']):.4f}")
    n = out['nara']
    e = np.array(n['after_err_ms'])
    print(f"NARA {n['tape']} tr4: before drift {n['before_o'][-1]:.1f} s; after median {np.median(e):+.3f} ms "
          f"rms {np.sqrt(np.mean(e**2)):.3f} ms over {len(e)} frames ({n['after_day_misread']} day-field misreads kept, {n['after_misread']} others dropped)")
