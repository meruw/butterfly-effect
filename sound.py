"""
Banda sonora del efecto mariposa, generada desde la propia simulacion.

Se escuchan 20 de las 3.000 atmosferas. Cada vez que una completa una vuelta
al atractor suena una nota: la altura sale de lo alto que llego la vuelta
(el maximo de z) y el lado estereo sale del ala en la que esta (el signo de x).

Mientras las atmosferas coinciden, las 20 tocan la misma melodia al unisono.
Cuando divergen, la melodia se deshace en 20 melodias distintas.

Uso:
  python sound.py --video out/efecto_mariposa.mp4
"""
import argparse
import math
import os
import subprocess
import wave

import numpy as np

import butterfly as bf

SR = 48000
NOTES = [62, 64, 66, 69, 71, 74, 76, 78, 81, 83]   # re mayor pentatonica, D4..B5
NVOICES = 20


def hz(m):
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


def step_times(ci, fps):
    """Instante de la pelicula en que se alcanza cada paso de simulacion."""
    u, idx = np.unique(ci, return_index=True)
    return u, (idx + 1) / fps


def note_events(sub, ci, fps):
    nsteps = int(ci[-1])
    u, tt = step_times(ci, fps)
    voices = np.linspace(0, sub.shape[1] - 1, NVOICES).round().astype(int)
    out = []
    for k, v in enumerate(voices):
        x = sub[:nsteps + 1, v, 0]
        z = sub[:nsteps + 1, v, 2]
        pk = np.nonzero((z[1:-1] > z[:-2]) & (z[1:-1] >= z[2:]) & (z[1:-1] > 24.0))[0] + 1
        t = np.interp(pk, u, tt)
        deg = np.clip(((z[pk] - 30.0) / 17.0 * len(NOTES)).astype(int), 0, len(NOTES) - 1)
        for ti, di, xi in zip(t, deg, x[pk]):
            out.append((float(ti), k, int(di), float(np.clip(xi / 14.0, -1.0, 1.0))))
    return out


def lowpass_fft(x, cutoff, order=4, highpass=None):
    X = np.fft.rfft(x)
    f = np.fft.rfftfreq(x.size, 1.0 / SR)
    g = 1.0 / (1.0 + (f / cutoff) ** order)
    if highpass:
        r = (f / highpass) ** 2
        g *= r / (1.0 + r)
    return np.fft.irfft(X * g, x.size)


def synth(sub, ci, ev, fps, seed=5):
    rng = np.random.default_rng(seed)
    dur = len(ci) / fps
    n = int(math.ceil(dur * SR))
    t = np.arange(n) / SR
    tg, tb, te = (ev[k] / fps for k in ("go", "burst", "end"))

    # --- notas: una por cada vuelta de cada atmosfera ---
    plk = np.zeros((n, 2))
    L = int(2.4 * SR)
    tau = np.arange(L) / SR
    env = np.exp(-tau / 0.50) * (1.0 - np.exp(-tau / 0.004))
    e2 = np.exp(-tau / 0.22)
    e3 = np.exp(-tau / 0.10)
    detune = 2.0 ** (rng.uniform(-3.0, 3.0, NVOICES) / 1200.0)   # +-3 cents
    octave = np.where(np.arange(NVOICES) % 2 == 0, 1.0, 0.5)
    evs = note_events(sub, ci, fps)
    for ti, k, di, pan in evs:
        i0 = int(ti * SR)
        m = min(L, n - i0)
        if m <= 0:
            continue
        f = hz(NOTES[di]) * detune[k] * octave[k]
        w = 2.0 * math.pi * f * tau[:m]
        s = np.sin(w) + 0.40 * np.sin(2.0 * w) * e2[:m] + 0.14 * np.sin(3.01 * w) * e3[:m]
        s *= env[:m] * (0.85 if octave[k] == 1.0 else 1.0)
        a = (pan + 1.0) * math.pi / 4.0
        plk[i0:i0 + m, 0] += s * math.cos(a)
        plk[i0:i0 + m, 1] += s * math.sin(a)
    # Al unisono las 20 voces se suman en fase y suenan mucho mas fuerte que
    # cuando van cada una por su lado: se nivela con una ganancia lenta.
    def moving(v, sec):
        w = int(sec * SR)
        c = np.cumsum(np.concatenate([[0.0], v]))
        lo = np.clip(np.arange(n) - w // 2, 0, n)
        hi = np.clip(np.arange(n) + w // 2, 0, n)
        return (c[hi] - c[lo]) / np.maximum(hi - lo, 1)

    rms = np.sqrt(moving((plk ** 2).sum(axis=1), 1.2))
    active = rms > 0.05 * rms.max()
    gain = np.clip((np.median(rms[active]) / np.maximum(rms, 1e-9)) ** 0.8, 0.3, 5.0)
    gain = moving(np.where(active, gain, 1.0), 0.4)
    plk *= gain[:, None]
    plk *= 0.10 / float(np.sqrt((plk[active] ** 2).mean()))

    # --- colchon grave ---
    pad = np.zeros(n)
    for m_, amp in ((38, 0.50), (45, 0.36), (50, 0.42), (57, 0.22)):
        for dt in (-0.0015, 0.0, 0.0017):
            f = hz(m_) * (1.0 + dt)
            ph = rng.uniform(0, 2 * math.pi)
            for h in (1, 2, 3, 4):
                pad += amp / h ** 1.8 * np.sin(2 * math.pi * f * h * t + ph * h)
    pad *= 0.5 + 0.12 * np.sin(2 * math.pi * 0.09 * t)
    pad_env = np.clip(t / 2.5, 0, 1) ** 1.5 * (1.0 + 0.35 * np.exp(-((t - tb) / 1.6) ** 2))
    pad *= pad_env
    pad *= 0.055 / float(np.sqrt((pad[int(3 * SR):] ** 2).mean()))
    # acorde final bajo el titulo
    chord = np.zeros(n)
    for m_, amp in ((62, 0.5), (66, 0.4), (69, 0.45), (74, 0.3)):
        for dt in (-0.002, 0.002):
            chord += amp * np.sin(2 * math.pi * hz(m_) * (1 + dt) * t + rng.uniform(0, 6.28))
    chord *= np.clip((t - (te - 5.2)) / 2.5, 0, 1) ** 2
    chord *= 0.05 / float(np.sqrt((chord[int((te - 2.0) * SR):] ** 2).mean()))

    # --- subida y golpe en el momento en que la luz se abre ---
    fx = np.zeros(n)
    T = 2.6
    r = np.clip((t - (tb - T)) / T, 0, 1)
    gate = (t < tb) * 1.0 + (t >= tb) * np.exp(-np.maximum(t - tb, 0) / 0.05)
    noise = lowpass_fft(rng.standard_normal(n), 2200.0, order=4, highpass=250.0)
    noise /= float(np.abs(noise).max())
    fx += 0.22 * noise * r ** 2.6 * gate
    fr = hz(50) * 2.0 ** (r ** 2)
    fx += 0.06 * np.sin(2 * math.pi * np.cumsum(fr) / SR) * r ** 2 * gate
    d = np.maximum(t - tb, 0.0)
    on = (t >= tb) * 1.0
    fi = 46.0 + 70.0 * np.exp(-d / 0.11)
    fx += 0.50 * on * np.sin(2 * math.pi * np.cumsum(fi * on) / SR) * np.exp(-d / 0.60)
    burst = lowpass_fft(rng.standard_normal(n), 900.0, order=2)
    burst /= float(np.abs(burst).max())
    fx += 0.22 * on * burst * np.exp(-d / 0.25)

    # --- reverberacion ---
    def reverb(x, sd):
        g = np.random.default_rng(sd)
        Lr = int(3.2 * SR)
        tr = np.arange(Lr) / SR
        ir = g.standard_normal(Lr) * np.exp(-tr / 0.42)
        ir[: int(0.02 * SR)] = 0.0
        ir = lowpass_fft(ir, 4500.0, order=2)
        ir /= math.sqrt(float((ir ** 2).sum()))
        N = 1 << int(math.ceil(math.log2(x.size + Lr)))
        return np.fft.irfft(np.fft.rfft(x, N) * np.fft.rfft(ir, N), N)[: x.size]

    mono = (pad + chord)[:, None]
    dry = plk + fx[:, None] + mono
    send = plk + 0.5 * fx[:, None] + 0.5 * mono
    wet = np.stack([reverb(send[:, 0], 11), reverb(send[:, 1], 12)], axis=1)
    mix = dry + 0.6 * wet

    fade = np.clip(t / 0.35, 0, 1) * np.clip((dur - t) / 0.6, 0, 1)
    mix *= fade[:, None]

    # --- arranque en frio: se oye por adelantado el momento de la divergencia ---
    n0 = int(round(bf.COLD_S * SR))
    a = int(round((tb + bf.COLD_AT) * SR))
    tc = np.arange(n0) / SR
    cold = mix[a:a + n0] * (np.clip(tc / 0.03, 0, 1) * np.clip((bf.COLD_S - tc) / 0.35, 0, 1))[:, None]
    full = np.concatenate([cold, mix], axis=0)
    # golpe grave en el corte del adelanto al inicio de la historia
    th = np.arange(full.shape[0]) / SR - bf.COLD_S
    onh = (th >= 0) * 1.0
    dh = np.maximum(th, 0.0)
    hit = 0.30 * onh * np.sin(2 * math.pi * np.cumsum((44.0 + 50.0 * np.exp(-dh / 0.09)) * onh) / SR) * np.exp(-dh / 0.45)
    full += hit[:, None]
    full *= 0.89 / float(np.abs(full).max())
    return full, evs


def write_wav(path, mix):
    pcm = (np.clip(mix, -1, 1) * 32767.0).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.tobytes())


def mux(video, wav, out):
    import imageio_ffmpeg
    tmp = out + ".tmp.mp4"
    r = subprocess.run([
        imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-i", video, "-i", wav, "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "copy", "-c:a", "aac", "-b:a", "256k", "-shortest", "-movflags", "+faststart", tmp],
        capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode(errors="replace")[-2000:])
    os.replace(tmp, out)


def add_to_video(video, sub, ci, ev, fps, wav=None):
    """Sintetiza la banda sonora y, si se da un video, la mezcla en el."""
    wav = wav or (os.path.splitext(video)[0] + ".wav")
    mix, evs = synth(sub, ci, ev, fps)
    os.makedirs(os.path.dirname(os.path.abspath(wav)) or ".", exist_ok=True)
    write_wav(wav, mix)
    rms = float(np.sqrt((mix ** 2).mean()))
    print(f"audio: {wav}  {mix.shape[0] / SR:.2f}s  notas {len(evs)}  pico {np.abs(mix).max():.2f}  rms {rms:.3f}")
    if video:
        mux(video, wav, video)
        print("video con audio:", video, f"{os.path.getsize(video) / 1e6:.1f} MB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", default="", help="MP4 al que se le agrega el audio (se reemplaza)")
    ap.add_argument("--wav", default="out/efecto_mariposa.wav")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--tail", type=float, default=10.0)
    args = ap.parse_args()

    x0 = bf.find_start()
    diag, cent, sub, frac = bf.presim(x0, 26000)
    nsub, ci, ev = bf.build_schedule(diag, args.fps, args.tail)
    add_to_video(args.video, sub, ci, ev, args.fps, wav=args.wav)


if __name__ == "__main__":
    main()
