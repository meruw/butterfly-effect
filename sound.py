"""
Banda sonora del efecto mariposa: la musica sigue a la mariposa.

Se escuchan 20 de las 3.000 atmosferas, y cada giro se oye en el momento en
que ocurre:

  - en lo alto de cada vuelta suena una nota aguda y en lo bajo una grave;
    el ritmo de la melodia es la velocidad a la que gira la atmosfera;
  - cada ala tiene su voz: la derecha es celesta, aguda y a la derecha;
    la izquierda es arpa, mas grave y a la izquierda;
  - cada cambio de ala cambia el acorde y trae una nota de bajo;
  - cuanto mas grande es la vuelta, mas alta (o mas baja) es la nota.

Mientras las atmosferas coinciden, las 20 tocan lo mismo al unisono. Cuando
divergen, cada una sigue su propia ala y la melodia se abre en 20 voces.

Que acordes le tocan a cada ala, el colchon y el glissando que sube hasta el
momento de la divergencia son composicion, no datos.

Uso:
  python sound.py --video out/efecto_mariposa.mp4
"""
import argparse
import bisect
import math
import os
import subprocess
import wave

import numpy as np

import butterfly as bf

SR = 48000
NVOICES = 20
LEADS = (0, 5, 10, 15, 19)          # voces que suenan en primer plano
BAR = 2.5                            # duracion de cada acorde del colchon tras la divergencia
RIGHT = ("D", "A")                   # acordes del ala derecha: alternan en cada visita
LEFT = ("Bm", "G")                   # acordes del ala izquierda

# Re mayor. Clases de altura de cada acorde: tres notas del acorde y una de color.
CHORDS = {
    "D": {"bass": 38, "pcs": (2, 6, 9, 4)},
    "A": {"bass": 33, "pcs": (9, 1, 4, 11)},
    "Bm": {"bass": 35, "pcs": (11, 2, 6, 9)},
    "G": {"bass": 31, "pcs": (7, 11, 2, 9)},
}
# (razon de frecuencia, amplitud, caida en s) por parcial, y ataque en s
TIMBRE = {
    "celesta": ([(1.0, 1.00, 0.75), (2.0, 0.28, 0.30), (4.0, 0.10, 0.12), (5.42, 0.05, 0.05)], 0.003),
    "harp": ([(1.0, 1.00, 0.95), (2.0, 0.45, 0.50), (3.0, 0.22, 0.30), (4.0, 0.10, 0.20)], 0.004),
    "spark": ([(1.0, 1.00, 0.40), (3.0, 0.18, 0.08)], 0.002),
    "bass": ([(1.0, 1.00, 1.10), (2.0, 0.55, 0.80), (3.0, 0.28, 0.50)], 0.025),
}


def hz(m):
    return 440.0 * 2.0 ** ((m - 69) / 12.0)


def pool(chord, lo, hi):
    pcs = CHORDS[chord]["pcs"]
    return [m for m in range(lo, hi + 1) if m % 12 in pcs]


def wing_visits(x):
    """Tramos de una trayectoria: (paso inicial, ala, numero de visita a esa ala)."""
    sg = np.where(x >= 0, 1, -1)
    starts = np.concatenate([[0], np.nonzero(sg[1:] != sg[:-1])[0] + 1])
    out, seen = [], {1: 0, -1: 0}
    for st in starts:
        w = int(sg[st])
        out.append((int(st), w, seen[w]))
        seen[w] += 1
    return out


def visit_chord(wing, k):
    return (RIGHT if wing > 0 else LEFT)[k % 2]


def chord_at(plan, t):
    name = plan[0][1]
    for t0, c in plan:
        if t0 <= t + 1e-9:
            name = c
    return name


def step_times(ci, fps):
    """Instante de la pelicula en que se alcanza cada paso de simulacion."""
    u, idx = np.unique(ci, return_index=True)
    return u, (idx + 1) / fps


class Mixer:
    def __init__(self, n, seed):
        self.n = n
        self.rng = np.random.default_rng(seed)
        self.bus = {}
        L = int(3.0 * SR)
        self.tau = np.arange(L) / SR

    def get(self, name):
        if name not in self.bus:
            self.bus[name] = np.zeros((self.n, 2))
        return self.bus[name]

    def note(self, bus, t0, midi, amp, pan=0.0, kind="celesta", cents=0.0):
        i0 = int(round(t0 * SR))
        if i0 < 0 or i0 >= self.n:
            return
        parts, attack = TIMBRE[kind]
        m = min(self.tau.size, self.n - i0)
        tau = self.tau[:m]
        f = hz(midi) * 2.0 ** (cents / 1200.0)
        s = np.zeros(m)
        for ratio, a, dec in parts:
            if f * ratio < 0.45 * SR:
                s += a * np.sin(2.0 * math.pi * f * ratio * tau) * np.exp(-tau / dec)
        s *= (1.0 - np.exp(-tau / attack)) * amp
        ang = (max(-1.0, min(1.0, pan)) + 1.0) * math.pi / 4.0
        b = self.get(bus)
        b[i0:i0 + m, 0] += s * math.cos(ang)
        b[i0:i0 + m, 1] += s * math.sin(ang)


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
    tg, ts, tb, tf, te = (ev[k] / fps for k in ("go", "slow", "burst", "fast", "end"))
    mx = Mixer(n, seed)
    nsteps = int(ci[-1])
    u, tt = step_times(ci, fps)
    voices = np.linspace(0, sub.shape[1] - 1, NVOICES).round().astype(int)
    detune = rng.uniform(-4.0, 4.0, NVOICES)

    def when(step):
        return float(np.interp(step, u, tt))

    # --- armonia: la marca la atmosfera central con sus cambios de ala ---
    lead_v = voices[NVOICES // 2]
    plan = [(when(st), visit_chord(w, k)) for st, w, k in wing_visits(sub[:nsteps + 1, lead_v, 0])]
    plan = [(t0, c) for t0, c in plan if t0 < tb + 0.4]
    t0, i = tb + 0.4, 0
    while t0 < te - 3.4 - 0.5 * BAR:                 # ya divergidas: las dos alas suenan a la vez
        plan.append((t0, ("D", "G")[i % 2])); t0 += BAR; i += 1
    plan.append((te - 3.4, "D"))

    # --- melodia: cada giro de cada atmosfera, en el instante en que ocurre ---
    count = 0
    for k, v in enumerate(voices):
        x = sub[:nsteps + 1, v, 0]
        z = sub[:nsteps + 1, v, 2]
        visits = wing_visits(x)
        vstart = [a[0] for a in visits]
        lead = k in LEADS
        top = np.nonzero((z[1:-1] > z[:-2]) & (z[1:-1] >= z[2:]) & (z[1:-1] > 24.0))[0] + 1
        bot = np.nonzero((z[1:-1] < z[:-2]) & (z[1:-1] <= z[2:]) & (z[1:-1] < 24.0))[0] + 1
        evs = [(int(p), True) for p in top] + ([(int(p), False) for p in bot] if lead else [])
        for p, high in sorted(evs):
            tn = when(p)
            if tn > te - 0.3:
                continue
            _, wing, nvis = visits[bisect.bisect_right(vstart, p) - 1]
            ch = visit_chord(wing, nvis)
            if high:                                   # lo alto del giro: cuanto mas alto, mas aguda
                ps = pool(ch, 69, 93) if wing > 0 else pool(ch, 57, 81)
                frac = (z[p] - 30.0) / 17.0
            else:                                      # lo bajo del giro: cuanto mas hondo, mas grave
                ps = pool(ch, 62, 76) if wing > 0 else pool(ch, 50, 64)
                frac = (z[p] - 4.0) / 16.0
            midi = ps[int(min(0.999, max(0.0, frac)) * len(ps))]
            amp = (1.0 if lead else 0.35) * (1.0 if high else 0.7) * rng.uniform(0.92, 1.05)
            mx.note("lead" if lead else "cloud", tn, midi, amp, wing * (0.55 if lead else 0.9),
                    "celesta" if wing > 0 else "harp", detune[k])
            count += 1
        if lead:                                       # destello en cada cambio de ala
            for st, wing, nvis in visits[1:]:
                tn = when(st)
                if tg + 0.8 < tn < te - 0.5:
                    ps = pool(visit_chord(wing, nvis), 86, 98)
                    mx.note("spark", tn, ps[int(rng.integers(0, len(ps)))], 0.5 * rng.uniform(0.7, 1.0), wing * 0.7, "spark")

    # --- bajo: la fundamental del acorde nuevo, en cada cambio ---
    lastb = -1.0
    for t0, ch in plan:
        if t0 - lastb >= 0.45 and t0 > 0.6:
            mx.note("bass", t0, CHORDS[ch]["bass"], 1.0, 0.0, "bass")
            lastb = t0

    # --- colchon: el acorde sostenido, con cruces suaves entre acordes ---
    pad = np.zeros(n)
    for i, (t0, ch) in enumerate(plan):
        t1 = plan[i + 1][0] if i + 1 < len(plan) else te + 1.0
        envp = np.clip((t - (t0 - 0.1)) / 0.3, 0, 1) * np.clip((t1 + 0.4 - t) / 0.55, 0, 1)
        sel = envp > 0
        if not sel.any():
            continue
        tones = pool(ch, 50, 69)[:5]
        if t0 >= te - 3.5:
            tones = [50, 57, 62, 64, 66, 69]              # re mayor con novena para el cierre
        s = np.zeros(int(sel.sum()))
        for m_ in tones:
            for dt in (-0.0016, 0.0017):
                f = hz(m_) * (1.0 + dt)
                ph = rng.uniform(0, 2 * math.pi)
                for h, a in ((1, 1.0), (2, 0.30), (3, 0.10)):
                    s += a * np.sin(2 * math.pi * f * h * t[sel] + ph * h)
        pad[sel] += s * envp[sel] / len(tones)
    swell = 0.55 + 0.45 * np.clip((t - ts) / max(0.1, tb - ts), 0, 1) ** 2      # crece hacia la divergencia
    swell = np.where(t >= tb, 1.0 + 0.5 * np.exp(-(t - tb) / 1.2), swell)
    pad *= swell * np.clip(t / 1.2, 0, 1) * (0.9 + 0.1 * np.sin(2 * math.pi * 0.11 * t))

    # --- glissando de arpa que sube hasta el momento en que la luz se parte ---
    penta = [m for m in range(62, 99) if m % 12 in (2, 4, 6, 9, 11)]
    g0, ng = tb - 0.62, len(penta)
    for i, m_ in enumerate(penta):
        tn = g0 + 0.62 * (i / ng) ** 0.8
        mx.note("gliss", tn, m_, 0.25 + 0.75 * i / ng, -0.7 + 1.4 * i / ng, "harp")
    for i, m_ in enumerate((50, 57, 62, 66, 69, 74, 78, 81, 86)):       # acorde abierto, arpegiado
        mx.note("gliss", tb + 0.012 * i, m_, 1.0, -0.6 + 0.15 * i, "harp" if m_ < 70 else "celesta")
    for i, m_ in enumerate((50, 57, 62, 64, 66, 69, 74, 78)):           # acorde de cierre
        mx.note("gliss", te - 3.4 + 0.03 * i, m_, 0.7, -0.5 + 0.14 * i, "harp" if m_ < 70 else "celesta")

    # --- aire: subida suave antes de la divergencia y brillo despues ---
    fx = np.zeros((n, 2))
    T = 2.4
    r = np.clip((t - (tb - T)) / T, 0, 1)
    gate = np.where(t < tb, 1.0, np.exp(-np.maximum(t - tb, 0) / 0.04))
    for chn in range(2):
        air = lowpass_fft(rng.standard_normal(n), 5200.0, order=4, highpass=900.0)
        air /= float(np.abs(air).max())
        d = np.maximum(t - tb, 0.0)
        fx[:, chn] += 0.16 * air * r ** 3 * gate
        fx[:, chn] += 0.20 * air * (t >= tb) * np.exp(-d / 0.9) * (1.0 - np.exp(-d / 0.01))
    d = np.maximum(t - tb, 0.0)
    on = (t >= tb) * 1.0
    boom = np.sin(2 * math.pi * np.cumsum((50.0 + 60.0 * np.exp(-d / 0.10)) * on) / SR) * np.exp(-d / 0.7) * on
    fx += (0.38 * boom)[:, None]

    # --- niveles: el unisono suma en fase y sonaria mucho mas fuerte que el arpegio ---
    def moving(v, sec):
        w = int(sec * SR)
        c = np.cumsum(np.concatenate([[0.0], v]))
        lo = np.clip(np.arange(n) - w // 2, 0, n)
        hi = np.clip(np.arange(n) + w // 2, 0, n)
        return (c[hi] - c[lo]) / np.maximum(hi - lo, 1)

    def level(x, target, ride=True):
        p = (x ** 2).sum(axis=1) if x.ndim == 2 else x ** 2
        rms = np.sqrt(moving(p, 1.2))
        active = rms > 0.05 * rms.max()
        if ride:
            g = np.clip((np.median(rms[active]) / np.maximum(rms, 1e-9)) ** 0.75, 0.35, 4.0)
            g = moving(np.where(active, g, 1.0), 0.4)
            x = x * (g[:, None] if x.ndim == 2 else g)
            p = (x ** 2).sum(axis=1) if x.ndim == 2 else x ** 2
        return x * (target / math.sqrt(float(p[active].mean())))

    mel = level(mx.get("lead") + mx.get("cloud"), 0.105)
    spark = level(mx.get("spark"), 0.020, ride=False)
    bass = level(mx.get("bass"), 0.050, ride=False)
    gliss = mx.get("gliss") * (0.30 / float(np.abs(mx.get("gliss")).max()))
    padl = level(pad, 0.048, ride=False)[:, None]

    def reverb(x, sd):
        g = np.random.default_rng(sd)
        Lr = int(4.0 * SR)
        tr = np.arange(Lr) / SR
        ir = g.standard_normal(Lr) * np.exp(-tr / 0.55)
        ir[: int(0.025 * SR)] = 0.0
        ir = lowpass_fft(ir, 6500.0, order=2, highpass=180.0)
        ir /= math.sqrt(float((ir ** 2).sum()))
        N = 1 << int(math.ceil(math.log2(x.size + Lr)))
        return np.fft.irfft(np.fft.rfft(x, N) * np.fft.rfft(ir, N), N)[: x.size]

    dry = mel + spark + bass + gliss + padl + fx
    send = 0.55 * mel + 1.0 * spark + 0.1 * bass + 0.7 * gliss + 0.45 * padl + 0.5 * fx
    wet = np.stack([reverb(send[:, 0], 11), reverb(send[:, 1], 12)], axis=1)
    mix = dry + 0.75 * wet
    mix *= (np.clip(t / 0.25, 0, 1) * np.clip((dur - t) / 0.6, 0, 1))[:, None]

    # --- arranque en frio: se oye por adelantado el momento de la divergencia ---
    n0 = int(round(bf.COLD_S * SR))
    a = int(round((tb + bf.COLD_AT) * SR))
    tc = np.arange(n0) / SR
    cold = mix[a:a + n0] * (np.clip(tc / 0.03, 0, 1) * np.clip((bf.COLD_S - tc) / 0.35, 0, 1))[:, None]
    full = np.concatenate([cold, mix], axis=0)
    full *= 0.89 / float(np.abs(full).max())
    return full, count


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
    mix, count = synth(sub, ci, ev, fps)
    os.makedirs(os.path.dirname(os.path.abspath(wav)) or ".", exist_ok=True)
    write_wav(wav, mix)
    rms = float(np.sqrt((mix ** 2).mean()))
    print(f"audio: {wav}  {mix.shape[0] / SR:.2f}s  notas {count}  pico {np.abs(mix).max():.2f}  rms {rms:.3f}")
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
