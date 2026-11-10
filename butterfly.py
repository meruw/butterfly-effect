"""
El efecto mariposa: 3.000 atmosferas de Lorenz renderizadas en CPU.

Cada "atmosfera" es una solucion del sistema de Lorenz (1963) que arranca a una
millonesima de distancia de las demas. Cada una tiene un color; superpuestas se
ven blancas, y al divergir la luz se abre en un espectro.

Uso:
  python butterfly.py --stills 0,8,15,27 --scale 0.5     # fotogramas de prueba
  python butterfly.py --out out/efecto_mariposa.mp4      # video completo, con audio
"""
import argparse
import bisect
import math
import os
import subprocess
import sys
import time
from multiprocessing import Pipe, Process, shared_memory

import numpy as np

# ----------------------------------------------------------------------------
# Parametros fisicos y de muestreo
# ----------------------------------------------------------------------------
SIGMA, RHO, BETA = 10.0, 28.0, 8.0 / 3.0   # valores clasicos de Lorenz
H = 0.0025                                 # paso de integracion (RK2, punto medio)
JMAX = 2400                                # longitud maxima de estela, en pasos
M = 3000                                   # numero de atmosferas
S = 400                                    # muestras de estela por atmosfera
EPS = 0.5e-6                               # semiancho de la perturbacion inicial
BLK = 4096                                 # tamano de bloque (para que quepa en cache)
DIRV = np.array([1.0, 1.0, 1.0]) / math.sqrt(3.0)
TARGET = np.array([0.0, 0.0, 24.5])
FOV_DEG = 38.0

COLD_S = 2.8          # duracion del arranque en frio (adelanto del momento de la divergencia)
REWIND_S = 1.3        # rebobinado que lleva del adelanto al punto de partida
PRE_S = COLD_S + REWIND_S
COLD_AT = 1.3         # el adelanto empieza este tiempo despues de que la nube se abre
PLUME_STEPS = 2400    # ventana de tiempo de la grafica de pronostico, en pasos

TEXT = {
    "es": {
        "hook": ["Basta un aleteo para que", "el mañana sea otro."],
        "credit": "MODELO DE CONVECCIÓN DE EDWARD LORENZ · 1963",
        "c2": ["3.000 atmósferas simuladas a la vez,", "cada una de un color."],
        "c2b": ["Diferencia inicial entre ellas:", "una millonésima."],
        "c2c": ["Si un ala es lluvia y la otra sol,", "todas dan el mismo pronóstico."],
        "c3": ["Mismas ecuaciones.", "Casi el mismo punto de partida."],
        "c4": ["Y cada una termina", "en un futuro distinto."],
        "c5": ["Por eso el pronóstico del tiempo", "es una probabilidad, no una certeza."],
        "title": "EL EFECTO MARIPOSA",
        "sub": "3.000 simulaciones del modelo de Lorenz (1963)",
        "hud": "SEPARACIÓN MÁXIMA ENTRE ATMÓSFERAS",
        "tag": "ATRACTOR DE LORENZ · 1963",
        "plume": "PRONÓSTICO POR CONJUNTOS · UNA LÍNEA POR ATMÓSFERA",
        "rain": "LLUVIA",
        "sun": "SOL",
        "dec": ",",
    },
    "en": {
        "hook": ["One wingbeat is enough", "to change tomorrow."],
        "credit": "EDWARD LORENZ'S CONVECTION MODEL · 1963",
        "c2": ["3,000 atmospheres simulated at once,", "each one a different color."],
        "c2b": ["Initial difference between them:", "one millionth."],
        "c2c": ["If one wing is rain and the other is sun,", "they all give the same forecast."],
        "c3": ["Same equations.", "Almost the same starting point."],
        "c4": ["And each one ends up", "in a different future."],
        "c5": ["That is why a weather forecast", "is a probability, not a certainty."],
        "title": "THE BUTTERFLY EFFECT",
        "sub": "3,000 simulations of the Lorenz model (1963)",
        "hud": "MAX SEPARATION BETWEEN ATMOSPHERES",
        "tag": "LORENZ ATTRACTOR · 1963",
        "plume": "ENSEMBLE FORECAST · ONE LINE PER ATMOSPHERE",
        "rain": "RAIN",
        "sun": "SUN",
        "dec": ".",
    },
}
EQUATIONS = "dx/dt = σ (y − x)        dy/dt = x (ρ − z) − y        dz/dt = x y − β z"


def smooth(x):
    x = min(1.0, max(0.0, x))
    return x * x * (3.0 - 2.0 * x)


def lowprio():
    """Prioridad baja para no estorbar a otras aplicaciones."""
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetPriorityClass(k.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
    except Exception:
        pass


def palette(t):
    stops = np.array([
        [0x3a, 0x5b, 0xff], [0x22, 0xd3, 0xff], [0xc8, 0xff, 0xf0], [0xff, 0xd7, 0x5e],
        [0xff, 0x8a, 0x3d], [0xff, 0x3d, 0x9a], [0x9b, 0x5c, 0xff]], dtype=np.float64) / 255.0
    pos = np.linspace(0.0, 1.0, len(stops))
    # Se equilibra para que la suma de todos los colores sea blanco neutro.
    ref = np.linspace(0.0, 1.0, 512)
    mean = np.array([np.interp(ref, pos, stops[:, i]).mean() for i in range(3)])
    gain = mean.mean() / mean
    return np.stack([np.interp(t, pos, stops[:, i]) * gain[i] for i in range(3)], axis=1)


# ----------------------------------------------------------------------------
# Integrador
# ----------------------------------------------------------------------------
class Lorenz:
    """Integra N copias del sistema con RK2. Solo avanza el tramo [a:a+c]."""

    def __init__(self, x, y, z, tmp=None):
        self.x, self.y, self.z = x, y, z
        self.px, self.py, self.pz = x.copy(), y.copy(), z.copy()
        self.t = [np.empty(tmp or x.size) for _ in range(9)]

    def save_prev(self, c, a=0):
        np.copyto(self.px[a:a + c], self.x[a:a + c])
        np.copyto(self.py[a:a + c], self.y[a:a + c])
        np.copyto(self.pz[a:a + c], self.z[a:a + c])

    def step(self, c, a=0):
        if c <= 0:
            return
        x, y, z = self.x[a:a + c], self.y[a:a + c], self.z[a:a + c]
        k1x, k1y, k1z, mx, my, mz, k2x, k2y, k2z = (a[:c] for a in self.t)
        np.subtract(y, x, out=k1x); k1x *= SIGMA
        np.subtract(RHO, z, out=k1y); k1y *= x; k1y -= y
        np.multiply(x, y, out=k1z); np.multiply(z, BETA, out=mz); k1z -= mz
        np.multiply(k1x, 0.5 * H, out=mx); mx += x
        np.multiply(k1y, 0.5 * H, out=my); my += y
        np.multiply(k1z, 0.5 * H, out=mz); mz += z
        np.subtract(my, mx, out=k2x); k2x *= SIGMA
        np.subtract(RHO, mz, out=k2y); k2y *= mx; k2y -= my
        np.multiply(mx, my, out=k2z); np.multiply(mz, BETA, out=k1x); k2z -= k1x
        k2x *= H; k2y *= H; k2z *= H
        x += k2x; y += k2y; z += k2z


def member_offsets():
    return np.linspace(-1.0, 1.0, M)


# ----------------------------------------------------------------------------
# Proceso trabajador: simula su parte de las particulas y las proyecta
# ----------------------------------------------------------------------------
def worker_main(conn, shm_name, wid, nw, W, Hh, scale, x0, seed):
    lowprio()
    shm = shared_memory.SharedMemory(name=shm_name)
    buf = np.ndarray((Hh, W, 3), dtype=np.float32, buffer=shm.buf)
    rng = np.random.default_rng(seed + wid)

    mem = np.arange(wid, M, nw)
    mw = mem.size
    # Retrasos muestreados con densidad ~ (1-u)^2: la estela se apaga sola.
    r = rng.random((mw, S - 1))
    lag = np.floor(JMAX * (1.0 - np.cbrt(1.0 - r))).astype(np.int64)
    lag = np.concatenate([np.zeros((mw, 1), np.int64), lag], axis=1).ravel()
    midx = np.repeat(mem, S)
    order = np.argsort(lag, kind="stable")
    lag = lag[order]
    midx = midx[order]
    n = lag.size

    s = member_offsets()[midx]
    sim = Lorenz(x0[0] + EPS * s * DIRV[0], x0[1] + EPS * s * DIRV[1], x0[2] + EPS * s * DIRV[2], tmp=BLK)
    # La cabeza de cada cometa brilla mas que su estela.
    col = palette((s + 1.0) * 0.5) * (1.0 + 3.0 * np.exp(-lag / 14.0))[:, None]
    cr, cg, cb = (np.ascontiguousarray(col[:, i]) for i in range(3))
    lagu = lag / float(JMAX)
    sig = (0.55 + 2.6 * np.exp(-lag / 40.0)) * scale

    POOL = 65536
    pn = BLK + POOL
    upool = rng.random(pn)
    gxp = rng.standard_normal(pn)
    gyp = rng.standard_normal(pn)
    ang = rng.random(pn) * 2.0 * math.pi
    rad = np.sqrt(rng.random(pn))
    dkx = rad * np.cos(ang)
    dky = rad * np.sin(ang)
    del ang, rad

    conn.send("ready")
    while True:
        msg = conn.recv()
        if msg is None:
            break
        nsub, n0, render, cam = msg
        idxs, wr, wg, wb = [], [], [], []
        passes = max(nsub, 1)
        if nsub > 0:
            nn = n0 + np.arange(passes)
            c_adv = np.searchsorted(lag, nn, "right")
            c_vis = np.searchsorted(lag, nn + 1, "right")
        else:
            c_adv = np.zeros(1, np.int64)
            c_vis = np.searchsorted(lag, np.array([n0]), "right")
        jvis = cam["jvis"] if render else JMAX
        cj = int(np.searchsorted(lag, jvis, "left")) if jvis < JMAX else n
        if render:
            C, R, U, F = cam["C"], cam["R"], cam["U"], cam["F"]
            fcl, ccx, ccy, focus, apx = cam["f"], cam["cx"], cam["cy"], cam["focus"], cam["ap"] * scale
            offs = rng.integers(0, POOL, (passes, 3))
        # Por bloques que caben en cache: cada bloque hace todos los subpasos
        # del fotograma antes de pasar al siguiente.
        for a in range(0, int(c_vis.max()), BLK):
            for k in range(passes):
                cv = min(BLK, int(c_vis[k]) - a)
                if cv <= 0:
                    continue
                sim.save_prev(cv, a)
                if nsub > 0:
                    sim.step(min(BLK, int(c_adv[k]) - a), a)
                if not render:
                    continue
                c = min(cv, cj - a)
                if c <= 0:
                    continue
                sl = slice(a, a + c)
                o1, o2, o3 = (int(v) for v in offs[k])
                X = sim.x[sl] - sim.px[sl]; X *= upool[o1:o1 + c]; X += sim.px[sl]
                Y = sim.y[sl] - sim.py[sl]; Y *= upool[o1:o1 + c]; Y += sim.py[sl]
                Z = sim.z[sl] - sim.pz[sl]; Z *= upool[o1:o1 + c]; Z += sim.pz[sl]
                X -= C[0]; Y -= C[1]; Z -= C[2]
                zc = X * F[0] + Y * F[1] + Z * F[2]
                xc = X * R[0] + Y * R[1] + Z * R[2]
                yc = X * U[0] + Y * U[1] + Z * U[2]
                np.maximum(zc, 1e-3, out=zc)
                inv = fcl / zc
                coc = np.abs(zc - focus); coc /= zc; coc *= apx
                sx = xc * inv; sx += ccx
                sx += coc * dkx[o2:o2 + c]; sx += sig[sl] * gxp[o3:o3 + c]
                sy = yc * inv; np.subtract(ccy, sy, out=sy)
                sy += coc * dky[o2:o2 + c]; sy += sig[sl] * gyp[o3:o3 + c]
                mask = (sx >= 0) & (sx < W) & (sy >= 0) & (sy < Hh) & (zc > 1.0)
                ix = sx[mask].astype(np.int64)
                iy = sy[mask].astype(np.int64)
                iy *= W; iy += ix
                idxs.append(iy)
                if jvis < JMAX:
                    wm = ((1.0 - lag[sl] / float(jvis)) / (1.0 - lagu[sl])) ** 2
                    wm = wm[mask]
                    wr.append(cr[sl][mask] * wm); wg.append(cg[sl][mask] * wm); wb.append(cb[sl][mask] * wm)
                else:
                    wr.append(cr[sl][mask]); wg.append(cg[sl][mask]); wb.append(cb[sl][mask])
        if render:
            if idxs:
                idx = np.concatenate(idxs)
                for ch, lst in enumerate((wr, wg, wb)):
                    b = np.bincount(idx, weights=np.concatenate(lst), minlength=W * Hh)
                    buf[:, :, ch] = b.reshape(Hh, W)
            else:
                buf[:] = 0.0
        conn.send(True)
    shm.close()


# ----------------------------------------------------------------------------
# Pre-simulacion de las cabezas: da el calendario y los datos de pantalla
# ----------------------------------------------------------------------------
def find_start():
    sim = Lorenz(np.array([1.0]), np.array([1.0]), np.array([1.0]))
    for _ in range(int(40.0 / H)):
        sim.step(1)
    for _ in range(int(40.0 / H)):
        sim.step(1)
        if sim.z[0] < 13.0 and abs(sim.x[0]) < 3.0 and sim.x[0] < 0:
            break
    return np.array([sim.x[0], sim.y[0], sim.z[0]])


def sub_members():
    return np.unique(np.concatenate([np.arange(0, M, 15), [M - 1]]))


def presim(x0, nsteps):
    """Devuelve, por paso: diagonal de la nube, centroide, estados de ~200
    atmosferas y fraccion de las 3.000 que estan en el ala derecha (x > 0)."""
    s = member_offsets()
    sim = Lorenz(x0[0] + EPS * s * DIRV[0], x0[1] + EPS * s * DIRV[1], x0[2] + EPS * s * DIRV[2])
    sel = sub_members()
    diag = np.empty(nsteps + 1)
    cent = np.empty((nsteps + 1, 3))
    sub = np.empty((nsteps + 1, sel.size, 3))
    frac = np.empty(nsteps + 1)
    for i in range(nsteps + 1):
        dx = sim.x.max() - sim.x.min(); dy = sim.y.max() - sim.y.min(); dz = sim.z.max() - sim.z.min()
        diag[i] = math.sqrt(dx * dx + dy * dy + dz * dz)
        cent[i] = (sim.x.mean(), sim.y.mean(), sim.z.mean())
        sub[i, :, 0] = sim.x[sel]; sub[i, :, 1] = sim.y[sel]; sub[i, :, 2] = sim.z[sel]
        frac[i] = float((sim.x > 0).mean())
        if i < nsteps:
            sim.step(M)
    return diag, cent, sub, frac


def build_schedule(diag, fps, tail_s):
    q = 60.0 / fps
    FAST, SLOW, NORM = 10.0 * q, 4.0 * q, 10.0 * q
    TH1, TH2 = 0.6, 20.0
    EXTRA = 700          # pasos en camara lenta despues de que la nube se abre

    def ramp(a, b, sec):
        n = max(1, int(round(sec * fps)))
        return [a + (b - a) * smooth((i + 1) / n) for i in range(n)]

    # La separacion oscila mucho a lo largo de la orbita: se usa el ultimo paso
    # en que estuvo por debajo del umbral, no el primero en que lo supera.
    n1 = int(np.nonzero(diag < TH1)[0][-1]) + 1
    n2 = int(np.nonzero(diag < TH2)[0][-1]) + 1 + EXTRA
    rates = [0.0] * int(round(0.3 * fps))
    ev = {"go": len(rates)}
    rates += ramp(0.0, FAST, 0.9)
    cum = sum(rates)
    look = 0.8 * fps * (FAST + SLOW) * 0.5
    while cum + look < n1:
        rates.append(FAST); cum += FAST
    ev["slow"] = len(rates)
    rates += ramp(FAST, SLOW, 0.8)
    cum = sum(rates)
    while cum < n2:
        rates.append(SLOW); cum += SLOW
    ev["fast"] = len(rates)
    rates += ramp(SLOW, NORM, 1.2)
    rates += [NORM] * int(round(tail_s * fps))
    ev["end"] = len(rates)
    ci = np.round(np.cumsum(rates)).astype(np.int64)
    nsub = np.diff(np.concatenate([[0], ci]))
    ev["burst"] = int(np.searchsorted(ci, n2 - EXTRA))
    return nsub, ci, ev


def hermite(keys, t):
    ts = [k[0] for k in keys]
    vs = np.array([k[1] for k in keys], dtype=np.float64)
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    i = bisect.bisect_right(ts, t) - 1

    def tan(j):
        if j == 0 or j == len(ts) - 1:
            return np.zeros(vs.shape[1])
        return (vs[j + 1] - vs[j - 1]) / (ts[j + 1] - ts[j - 1])

    h = ts[i + 1] - ts[i]
    u = (t - ts[i]) / h
    m0, m1 = tan(i) * h, tan(i + 1) * h
    return ((2 * u ** 3 - 3 * u ** 2 + 1) * vs[i] + (u ** 3 - 2 * u ** 2 + u) * m0
            + (-2 * u ** 3 + 3 * u ** 2) * vs[i + 1] + (u ** 3 - u ** 2) * m1)


def camera_keys(tg, ts, tb, tf, te, p0):
    cx, cy, cz = TARGET
    #    t            az      el    dist  apert.  objetivo (x, y, z)
    return [
        (0.0,       [-100.0, 4.0, 48.0, 20.0, p0[0], p0[1], p0[2]]),      # primer plano del punto inicial
        (tg,        [-100.0, 4.0, 45.0, 20.0, p0[0], p0[1], p0[2]]),
        (tg + 2.6,  [-97.0, 6.0, 100.0, 12.0, cx, cy, cz]),               # se aleja y revela la mariposa
        (ts,        [-86.0, 9.0, 112.0, 12.0, cx, cy, cz]),
        (tb,        [-78.0, 11.0, 80.0, 20.0, 0.0, 0.0, 19.0]),           # se acerca a donde la luz se parte
        (tf,        [-66.0, 14.0, 92.0, 16.0, cx, cy, cz]),
        (tf + 3.5,  [-80.0, 24.0, 104.0, 13.0, cx, cy, cz]),
        (te - 3.5,  [-100.0, 14.0, 110.0, 11.0, cx, cy, cz]),
        (te,        [-92.0, 8.0, 120.0, 9.0, cx, cy, cz]),
    ]


def camera_basis(az, el, dist, target):
    a, e = math.radians(az), math.radians(el)
    C = target + dist * np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
    F = target - C
    F /= np.linalg.norm(F)
    R = np.cross(F, np.array([0.0, 0.0, 1.0]))
    R /= np.linalg.norm(R)
    U = np.cross(R, F)
    return C, R, U, F


# ----------------------------------------------------------------------------
# Texto
# ----------------------------------------------------------------------------
def load_font(names, size, variation=None):
    from PIL import ImageFont
    base = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for nme in names:
        idx = 0
        if isinstance(nme, tuple):
            nme, idx = nme
        p = os.path.join(base, nme)
        if os.path.exists(p):
            f = ImageFont.truetype(p, size, index=idx)
            if variation:
                try:
                    f.set_variation_by_name(variation)
                except Exception:
                    pass
            return f
    return ImageFont.load_default(size)


def make_layer(lines, font, color=(255, 255, 255), tracking=0.0, gap=0.06, shadow=0.8, align="c"):
    """Devuelve (rgb premultiplicado, alfa, margen) con sombra suave para legibilidad."""
    from PIL import Image, ImageDraw, ImageFilter

    def width(sv):
        return font.getlength(sv) + tracking * max(0, len(sv) - 1)

    asc, desc = font.getmetrics()
    lh = asc + desc
    pad = int(lh * 0.7)
    tw = max(width(l) for l in lines)
    Wd = int(tw + 2 * pad)
    Ht = int(len(lines) * lh + (len(lines) - 1) * lh * gap + 2 * pad)
    img = Image.new("L", (Wd, Ht), 0)
    d = ImageDraw.Draw(img)
    y = pad
    for l in lines:
        x = (Wd - width(l)) * 0.5 if align == "c" else (pad if align == "l" else Wd - pad - width(l))
        if tracking == 0:
            d.text((x, y), l, font=font, fill=255)
        else:
            for ch in l:
                d.text((x, y), ch, font=font, fill=255)
                x += font.getlength(ch) + tracking
        y += lh * (1 + gap)
    a = np.asarray(img, dtype=np.float32) / 255.0
    sh = np.asarray(img.filter(ImageFilter.GaussianBlur(pad * 0.3)), dtype=np.float32) / 255.0
    sh = np.clip(sh * 2.2, 0, 1) * shadow
    alpha = a + sh * (1 - a)
    rgb = a[..., None] * (np.array(color, dtype=np.float32) / 255.0)
    return rgb, alpha, pad


def comp(frame, layer, x, y, a, anchor="c"):
    """Compone una capa de texto. x es el centro ('c'), el borde izquierdo
    del texto ('l') o el derecho ('r'); y es siempre el centro vertical."""
    if a <= 0.001:
        return
    rgb, alpha, pad = layer
    h, w = alpha.shape
    if anchor == "c":
        x0 = int(round(x - w / 2))
    elif anchor == "l":
        x0 = int(round(x - pad))
    else:
        x0 = int(round(x - w + pad))
    y0 = int(round(y - h / 2))
    fx0, fy0 = max(0, x0), max(0, y0)
    fx1, fy1 = min(frame.shape[1], x0 + w), min(frame.shape[0], y0 + h)
    if fx1 <= fx0 or fy1 <= fy0:
        return
    sl = (slice(fy0 - y0, fy1 - y0), slice(fx0 - x0, fx1 - x0))
    reg = frame[fy0:fy1, fx0:fx1]
    reg *= (1.0 - alpha[sl] * a)[..., None]
    reg += rgb[sl] * a


# ----------------------------------------------------------------------------
# Revelado: de densidad lineal a imagen
# ----------------------------------------------------------------------------
class Developer:
    K, WP, SAT = 40.0, 8.0, 0.30

    def __init__(self, W, Hh, scale, cy):
        self.W, self.Hh, self.scale = W, Hh, scale
        self.prev = None
        self.ref = None
        yy, xx = np.mgrid[0:Hh, 0:W].astype(np.float32)
        r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - Hh / 2) / (Hh / 2)) ** 2)
        self.vig = np.clip(1.0 - 0.20 * r ** 2.4, 0.0, 1.0)[..., None].astype(np.float32)
        rc = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - cy) / (Hh / 2)) ** 2)
        g = np.exp(-(rc * 1.1) ** 2)[..., None]
        self.bg = (np.array([0.010, 0.011, 0.024], np.float32)
                   + g * np.array([0.022, 0.028, 0.060], np.float32)).astype(np.float32)
        rng = np.random.default_rng(99)
        self.dither = [((rng.random((Hh, W, 3), dtype=np.float32) - 0.5) / 255.0) for _ in range(4)]
        self.i = 0

    def reset(self):
        self.prev = None
        self.ref = None           # tras un salto, la exposicion se mide de nuevo

    def __call__(self, acc, persist, mask=None):
        import cv2
        W, Hh = self.W, self.Hh
        hdr = acc if (self.prev is None or persist <= 0) else acc + persist * self.prev
        self.prev = hdr
        lum = hdr.max(axis=2)
        nz = lum[::2, ::2]
        nz = nz[nz > 0]
        if nz.size:
            ref = float(np.percentile(nz, 99.0)) if nz.size > 400 else float(nz.max())
            self.ref = ref if self.ref is None else math.exp(0.88 * math.log(self.ref) + 0.12 * math.log(ref))
        if self.ref is None:
            self.ref = 1.0
        l = lum * (1.0 / self.ref)
        Lm = np.log1p(self.K * l) / math.log1p(self.K * self.WP)
        np.clip(Lm, 0.0, 1.15, out=Lm)
        ratio = Lm / np.maximum(lum, 1e-20)
        col = hdr * ratio[..., None]
        col += (col - col.mean(axis=2, keepdims=True)) * self.SAT
        np.maximum(col, 0.0, out=col)
        d = np.clip((Lm - 0.58) / 0.55, 0.0, 1.0)
        d *= d
        col += (Lm[..., None] - col) * d[..., None]
        if mask is not None:
            col *= mask          # la luz del atractor se apaga bajo la grafica; el fondo no
        small = cv2.resize(col, (W // 4, Hh // 4), interpolation=cv2.INTER_AREA)
        b1 = cv2.GaussianBlur(small, (0, 0), 2.2 * self.scale + 0.3)
        b2 = cv2.GaussianBlur(small, (0, 0), 10.0 * self.scale + 0.5)
        bloom = 0.45 * b1 + 0.55 * b2
        # Destello anamorfico: solo las zonas casi blancas se estiran en horizontal.
        hl = np.clip(small.min(axis=2) - 0.80, 0.0, None)
        k = max(3, int(W // 4 * 0.26)) | 1
        st = cv2.blur(cv2.blur(hl, (k, 1)), (k, 1))
        st = cv2.GaussianBlur(st, (0, 0), 0.7)
        st = 0.30 * np.tanh(st * 26.0)
        bloom += st[..., None] * np.array([0.55, 0.80, 1.0], np.float32)
        col += cv2.resize(bloom, (W, Hh), interpolation=cv2.INTER_LINEAR)
        np.clip(col, 0.0, 1.0, out=col)
        col = 1.0 - (1.0 - col) * (1.0 - self.bg)
        col *= self.vig
        return col

    def finish(self, col, fade):
        self.i += 1
        if fade < 1.0:
            col *= fade
        col += self.dither[self.i % 4]
        np.clip(col, 0.0, 1.0, out=col)
        return (col * 255.0 + 0.5).astype(np.uint8)


# ----------------------------------------------------------------------------
# Grafica de pronostico por conjuntos: x(t) de ~200 atmosferas
# ----------------------------------------------------------------------------
class Plume:
    PER_PX = 5

    def __init__(self, sub, rect, scale):
        self.sx = np.ascontiguousarray(sub[:, :, 0].T)          # (atmosferas, pasos)
        s = -1.0 + 2.0 * sub_members() / (M - 1)
        self.col = palette((s + 1.0) * 0.5).astype(np.float32)
        self.x0, self.y0, self.x1, self.y1 = rect
        self.pw, self.ph = self.x1 - self.x0, self.y1 - self.y0
        self.scale = scale
        self.rng = np.random.default_rng(31)

    def render(self, s_now):
        import cv2
        pw, ph = self.pw, self.ph
        K = self.PER_PX * pw
        u = (np.arange(K) + self.rng.random(K)) / K
        sp = s_now - PLUME_STEPS * (1.0 - u)
        ok = sp >= 0
        if not ok.any() or s_now < 1:
            return None
        sp, u = sp[ok], u[ok]
        i0 = np.minimum(np.floor(sp).astype(np.int64), s_now - 1)
        fr = sp - i0
        xv = self.sx[:, i0] * (1.0 - fr) + self.sx[:, i0 + 1] * fr
        yy = (0.5 - xv / 46.0) * ph + self.rng.normal(0.0, 0.6 * self.scale, xv.shape)
        iy = np.floor(yy).astype(np.int64)
        ix = np.minimum((u * pw).astype(np.int64), pw - 1)
        w = (1.0 + 2.5 * np.exp(-(1.0 - u) / 0.012)) * np.clip(u / 0.10, 0.0, 1.0)
        mask = (iy >= 0) & (iy < ph)
        idx = (iy * pw + ix[None, :])[mask]
        wt = np.broadcast_to(w[None, :], xv.shape)[mask]
        cidx = np.broadcast_to(np.arange(xv.shape[0])[:, None], xv.shape)[mask]
        d = np.stack([np.bincount(idx, weights=wt * self.col[cidx, ch], minlength=pw * ph).reshape(ph, pw)
                      for ch in range(3)], axis=2).astype(np.float32)
        lum = d.max(axis=2)
        l = lum / self.PER_PX
        Lm = np.where(l < 1.0, 0.5 * l, 0.5 * np.power(np.maximum(l, 1.0), 0.13))
        np.clip(Lm, 0.0, 1.0, out=Lm)
        img = d * (Lm / np.maximum(lum, 1e-9))[..., None]
        ds = np.clip((Lm - 0.62) / 0.38, 0.0, 1.0) ** 2
        img += (Lm[..., None] - img) * ds[..., None]
        img = cv2.GaussianBlur(img, (0, 0), 0.55 * self.scale + 0.2)
        img += 0.55 * cv2.GaussianBlur(img, (0, 0), 4.0 * self.scale + 0.5)
        return np.clip(img, 0.0, 1.0)


def fmt_sep(v, dec):
    return f"{v:.6f}".replace(".", dec)


# ----------------------------------------------------------------------------
# Granja de procesos: una pasada completa por la simulacion
# ----------------------------------------------------------------------------
def run_pass(P, wanted, sink, label, mask=None):
    """Simula desde el inicio y entrega a sink(f, capa) los fotogramas pedidos.
    wanted es un conjunto de fotogramas o None para todos."""
    W, Hh, scale, nw = P["W"], P["Hh"], P["scale"], P["workers"]
    nsub, ci = P["nsub"], P["ci"]
    last = len(nsub) - 1 if wanted is None else max(wanted)
    shms, conns, procs = [], [], []
    for w in range(nw):
        shm = shared_memory.SharedMemory(create=True, size=Hh * W * 3 * 4)
        a, b = Pipe()
        p = Process(target=worker_main, args=(b, shm.name, w, nw, W, Hh, scale, P["x0"], 1234), daemon=True)
        p.start()
        shms.append(shm); conns.append(a); procs.append(p)
    bufs = [np.ndarray((Hh, W, 3), dtype=np.float32, buffer=s.buf) for s in shms]
    for c in conns:
        c.recv()
    dev = Developer(W, Hh, scale, P["cy"])
    acc = np.empty((Hh, W, 3), dtype=np.float32)
    prev = -10
    t0 = time.time()
    for f in range(last + 1):
        render = wanted is None or f in wanted
        cam, persist = P["cam"](f)
        msg = (int(nsub[f]), int(ci[f - 1]) if f > 0 else 0, render, cam)
        for c in conns:
            c.send(msg)
        for c in conns:
            c.recv()
        if render:
            if f != prev + 1:
                dev.reset()
            prev = f
            np.copyto(acc, bufs[0])
            for b in bufs[1:]:
                acc += b
            sink(f, dev(acc.copy(), persist, mask), dev)
        if (f + 1) % 60 == 0 or f == last:
            el = time.time() - t0
            print(f"[{label}] fotograma {f + 1}/{last + 1}  {el / (f + 1):.2f}s/f  faltan {el / (f + 1) * (last - f) / 60:.1f} min")
            sys.stdout.flush()
    for c in conns:
        c.send(None)
    for p in procs:
        p.join(timeout=10)
    for s in shms:
        s.close()
        s.unlink()


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/efecto_mariposa.mp4")
    ap.add_argument("--scale", type=float, default=1.0, help="1.0 = 1080x1080")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--stills", default="", help="segundos del video separados por coma; guarda PNG en vez de video")
    ap.add_argument("--lang", default="es", help="es, en o es,en: un video por idioma en una sola pasada")
    ap.add_argument("--tail", type=float, default=10.0, help="segundos del acto final")
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--trail", type=int, default=100, help="longitud de estela (pasos) en el acto final")
    ap.add_argument("--no-audio", action="store_true", help="no sintetiza ni mezcla la banda sonora")
    ap.add_argument("--info", action="store_true", help="solo muestra el calendario")
    args = ap.parse_args()
    lowprio()
    import cv2
    cv2.setNumThreads(4)

    scale = args.scale
    W = Hh = int(round(1080 * scale / 2) * 2)
    fps = args.fps
    langs = [l.strip() for l in args.lang.split(",") if l.strip() in TEXT] or ["es"]

    t0 = time.time()
    x0 = find_start()
    diag, cent, sub, frac = presim(x0, 26000)
    nsub, ci, ev = build_schedule(diag, fps, args.tail)
    NF = len(nsub)
    tg, ts, tf, te, tb = (ev[k] / fps for k in ("go", "slow", "fast", "end", "burst"))
    N0 = int(round(COLD_S * fps))
    NR = int(round(REWIND_S * fps))
    fc0 = int(round((tb + COLD_AT) * fps))
    print(f"inicio {x0.round(3)}  presim {time.time() - t0:.1f}s")
    print(f"total {PRE_S + te:.1f}s = adelanto {COLD_S}s + rebobinado {REWIND_S}s + {NF} fotogramas  "
          f"go {tg:.2f}  slow {ts:.2f}  burst {tb:.2f}  fast {tf:.2f}  pasos {ci[-1]}")
    if args.info:
        for sec in range(0, int(te) + 1):
            f = min(NF - 1, sec * fps)
            print(f"  t={sec:2d}s paso={ci[f]:6d} sep={diag[ci[f]]:.6f} lluvia={100 * frac[ci[f]]:.0f}%")
        return

    # --- composicion de la pantalla (fracciones del alto) ---
    cy_att = 0.425 * Hh
    focal = (0.80 * Hh / 2) / math.tan(math.radians(FOV_DEG / 2))
    keys = camera_keys(tg, ts, tb, tf, te, x0)

    def cam_at(f):
        t = f / fps
        az, el, dist, apv, tx, ty, tz = hermite(keys, t)
        C, R, U, F = camera_basis(az, el, dist, np.array([tx, ty, tz]))
        zhead = float(np.dot(cent[int(ci[f])] - C, F))
        fb = smooth((t - ts) / max(1e-6, (tf - ts)))
        focus = zhead * (1 - fb) + dist * fb
        # Tras abrirse la nube, las estelas se acortan: cada atmosfera pasa a ser
        # un cometa corto y los colores dejan de mezclarse en blanco.
        jv = int(round(JMAX + (args.trail - JMAX) * smooth((t - tb - 1.2) / max(1e-6, tf + 2.2 - tb - 1.2))))
        cam = {"C": C, "R": R, "U": U, "F": F, "f": focal, "cx": W / 2, "cy": cy_att,
               "focus": focus, "ap": apv, "jvis": jv}
        return cam, 0.12 + 0.38 * smooth((t - tf) / 2.0)

    P = {"W": W, "Hh": Hh, "scale": scale, "workers": args.workers, "nsub": nsub, "ci": ci,
         "x0": x0, "cy": cy_att, "cam": cam_at}

    def px(v):
        return max(8, int(round(v * scale)))

    def trk(v):
        return v * scale

    # --- tipografia ---
    serif = [("Sitka.ttc", 4), "georgia.ttf", "times.ttf"]
    sans = ["bahnschrift.ttf", "seguisb.ttf", "arial.ttf"]
    f_cap = load_font(serif, px(42))
    f_hook = load_font(serif, px(74))
    f_eq = load_font(["cambriai.ttf", "timesi.ttf"], px(27))
    f_title = load_font(sans, px(60), "SemiBold")
    f_lab = load_font(sans, px(17), "SemiBold")
    f_pct = load_font(sans, px(46), "SemiBold")
    f_num = load_font(["consola.ttf", "cour.ttf"], px(28))
    ink = (246, 244, 238)
    cool, warm, dim = (132, 196, 255), (255, 206, 112), (150, 166, 198)

    # Los tres primeros textos se reparten el primer acto; cada uno dura mas de tres segundos.
    cap_y = 0.098 * Hh
    a1 = tg + 0.9
    third = (ts - 1.3 - a1) / 3.0
    t_lab = a1 + 2 * third          # el marcador lluvia/sol aparece con su subtitulo

    def make_texts(T):
        caps = [
            (make_layer(T["c2"], f_cap, ink), W / 2, cap_y, a1, a1 + third - 0.1),
            (make_layer(T["c2b"], f_cap, ink), W / 2, cap_y, a1 + third, a1 + 2 * third - 0.1),
            (make_layer(T["c2c"], f_cap, ink), W / 2, cap_y, a1 + 2 * third, ts - 1.3),
            (make_layer(T["c3"], f_cap, ink), W / 2, cap_y, ts - 1.2, tb - 0.2),
            # Las ecuaciones entran aqui, cuando el texto habla de ellas, y no al principio.
            (make_layer([EQUATIONS], f_eq, (190, 215, 255)), W / 2, 0.168 * Hh, ts - 0.8, tb - 0.2),
            (make_layer([T["credit"]], f_lab, dim, tracking=trk(2.0), shadow=0.6), W / 2, 0.203 * Hh, ts - 0.6, tb - 0.2),
            (make_layer(T["c4"], f_cap, ink), W / 2, cap_y, tb + 0.3, tf + 2.3),
            (make_layer(T["c5"], f_cap, ink), W / 2, cap_y, tf + 2.9, te - 3.7),
            (make_layer([T["title"]], f_title, ink, tracking=trk(8)), W / 2, 0.088 * Hh, te - 3.3, te + 1.0),
            (make_layer([T["sub"]], f_lab, dim, tracking=trk(1.6)), W / 2, 0.142 * Hh, te - 2.9, te + 1.0),
        ]
        return {
            "dec": T["dec"], "caps": caps,
            "hook": make_layer(T["hook"], f_hook, ink, shadow=0.9),
            "hud": make_layer([T["hud"]], f_lab, dim, tracking=trk(2.0), shadow=0.6),
            "tag": make_layer([T["tag"]], f_lab, dim, tracking=trk(2.0), shadow=0.6),
            "plume": make_layer([T["plume"]], f_lab, dim, tracking=trk(2.0), shadow=0.6),
            "rain": make_layer([T["rain"]], f_lab, cool, tracking=trk(2.4), shadow=0.6),
            "sun": make_layer([T["sun"]], f_lab, warm, tracking=trk(2.4), shadow=0.6),
        }

    texts = [make_texts(TEXT[l]) for l in langs]

    mx = 0.055 * W
    prect = (int(mx), int(0.738 * Hh), int(0.775 * W), int(0.874 * Hh))
    plume = Plume(sub, prect, scale)
    yy = np.arange(Hh, dtype=np.float32)
    att_mask = (1.0 - 0.90 * np.clip((yy - 0.66 * Hh) / (0.07 * Hh), 0, 1) ** 2)[:, None, None]
    foot_y = 0.940 * Hh

    def base_main(f, col):
        """Lo que no depende del idioma: la grafica de pronostico y sus guias."""
        t = f / fps
        ap_ = smooth((t - tg - 0.2) / 0.8)
        if ap_ > 0:
            x0p, y0p, x1p, y1p = prect
            ym = (y0p + y1p) // 2
            col[ym:ym + 1, x0p:x1p] += 0.07 * ap_
            col[y0p:y1p, x1p:x1p + 1] += 0.10 * ap_
            pim = plume.render(int(ci[f]))
            if pim is not None:
                reg = col[y0p:y1p, x0p:x1p]
                reg[:] = 1.0 - (1.0 - np.clip(reg, 0, 1)) * (1.0 - pim * ap_)
        return ap_

    def compose_main(f, col, dev, X, ap_):
        t = f / fps
        s_now = int(ci[f])
        if ap_ > 0:
            comp(col, X["plume"], mx, 0.716 * Hh, ap_ * 0.9, anchor="l")
            al = smooth((t - t_lab) / 0.6)
            if al > 0:
                pr = int(round(100.0 * frac[s_now]))
                lx = 0.803 * W
                comp(col, X["rain"], lx, 0.752 * Hh, al, anchor="l")
                comp(col, make_layer([f"{pr} %"], f_pct, cool, shadow=0.6), lx, 0.787 * Hh, al, anchor="l")
                comp(col, X["sun"], lx, 0.828 * Hh, al, anchor="l")
                comp(col, make_layer([f"{100 - pr} %"], f_pct, warm, shadow=0.6), lx, 0.863 * Hh, al, anchor="l")
            # pie: separacion real medida en la simulacion
            sp = sub[s_now]
            dd = sp[:, None, :] - sp[None, :, :]
            sep = float(np.sqrt((dd * dd).sum(axis=2).max()))
            comp(col, X["hud"], mx, foot_y, ap_ * 0.9, anchor="l")
            num = make_layer([fmt_sep(sep, X["dec"])], f_num, ink, shadow=0.6)
            comp(col, num, mx + X["hud"][1].shape[1] - 2 * X["hud"][2] + px(16), foot_y, ap_, anchor="l")
            comp(col, X["tag"], W - mx, foot_y, ap_ * 0.9, anchor="r")
        for layer, cx, cyy, b0, b1 in X["caps"]:
            comp(col, layer, cx, cyy, smooth((t - b0) / 0.5) * smooth((b1 - t) / 0.5))
        # Sin fundido de entrada: el rebobinado termina justo en este punto de luz.
        return dev.finish(col, smooth((te - t) / 0.45 + 0.02))

    def compose_pre(k, col, dev, X):
        """Adelanto y rebobinado: solo la pregunta, que se va al empezar a rebobinar."""
        t = k / fps
        comp(col, X["hook"], W / 2, 0.80 * Hh, smooth((COLD_S + 0.3 - t) / 0.35))
        return dev.finish(col, 1.0)

    # --- salidas: un video por idioma ---
    out = args.out
    outdir = os.path.dirname(os.path.abspath(out)) or "."
    os.makedirs(outdir, exist_ok=True)
    stem, ext = os.path.splitext(out)
    outs = [out if i == 0 else f"{stem}_{l}{ext}" for i, l in enumerate(langs)]
    stills = [float(v) for v in args.stills.split(",") if v.strip()]

    # Rebobinado: fotogramas de la propia pelicula, del final del adelanto hacia atras hasta
    # el punto de partida. Arranca despacio, corre y frena al llegar.
    f_last = fc0 + N0 - 1
    rew = [int(round(f_last * (1.0 - smooth((j + 1) / NR)))) for j in range(NR)]
    pre_k, main_f = None, None            # None = todos
    if stills:
        pre_k = sorted({int(round(v * fps)) for v in stills if v < PRE_S})
        main_f = sorted({min(NF - 1, int(round((v - PRE_S) * fps))) for v in stills if v >= PRE_S})

    def save_png(name, img, i):
        p = os.path.join(outdir, name if i == 0 else name.replace(".png", f"_{langs[i]}.png"))
        cv2.imwrite(p, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
        print("guardado", p)

    ffs = []
    if not stills:
        import imageio_ffmpeg
        for o in outs:
            log = open(os.path.splitext(o)[0] + ".ffmpeg.log", "wb")
            ffs.append(subprocess.Popen([
                imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", f"{W}x{Hh}", "-r", str(fps), "-i", "-", "-an",
                "-vf", "scale=out_color_matrix=bt709:out_range=tv",
                "-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-movflags", "+faststart", o], stdin=subprocess.PIPE, stdout=log, stderr=log))

    def emit(name, make):
        """make(i) devuelve el fotograma del idioma i; va a su video o a un PNG."""
        for i in range(len(langs)):
            img = make(i)
            if stills:
                save_png(name, img, i)
            else:
                ffs[i].stdin.write(img.tobytes())

    # --- pasada 1: adelanto y rebobinado (fotogramas que la pelicula mostrara despues) ---
    need_k = range(N0 + NR) if pre_k is None else pre_k
    src = {k: (fc0 + k if k < N0 else rew[k - N0]) for k in need_k}      # fotograma de pelicula de cada uno
    if src:
        wanted, keep = set(), set(src.values())
        for k, f in src.items():
            wanted.update(range(max(0, f - 5), f + 1) if k < N0 else [f])
        store = {}

        def sink_pre(f, col, dev):
            if f in keep:
                store[f] = (np.clip(col, 0, 1) * 255.0 + 0.5).astype(np.uint8)

        run_pass(P, wanted, sink_pre, "adelanto")
        dev0 = Developer(W, Hh, scale, cy_att)
        prev = None
        for k in sorted(src):
            col = store[src[k]].astype(np.float32) / 255.0
            if k >= N0 and prev is not None:
                col = 0.6 * col + 0.4 * prev          # estela de movimiento al rebobinar
            prev = col
            emit(f"still_{k / fps:05.2f}s.png", lambda i, c=col, k=k: compose_pre(k, c.copy(), dev0, texts[i]))
        store.clear()

    # --- pasada 2: la pelicula ---
    if main_f is None or len(main_f):
        wanted = None
        if main_f is not None:
            wanted = set()
            for f in main_f:
                wanted.update(range(max(0, f - 5), f + 1))

        def sink_main(f, col, dev):
            if main_f is not None and f not in main_f:
                return
            ap_ = base_main(f, col)
            emit(f"still_{PRE_S + f / fps:05.2f}s.png", lambda i: compose_main(f, col.copy(), dev, texts[i], ap_))

        run_pass(P, wanted, sink_main, "pelicula", mask=att_mask)

    for i, ff in enumerate(ffs):
        ff.stdin.close()
        ff.wait()
        print("video:", outs[i], f"{os.path.getsize(outs[i]) / 1e6:.1f} MB")
        if not args.no_audio:
            import sound
            sound.add_to_video(outs[i], sub, ci, ev, fps)
    print(f"total {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
