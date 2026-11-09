"""
El efecto mariposa: 3.000 atmosferas de Lorenz renderizadas en CPU.

Cada "atmosfera" es una solucion del sistema de Lorenz (1963) que arranca a una
millonesima de distancia de las demas. Cada una tiene un color; superpuestas se
ven blancas, y al divergir la luz se abre en un espectro.

Uso:
  python butterfly.py --stills 3,8,12,20 --scale 0.5     # fotogramas de prueba
  python butterfly.py --out out/efecto_mariposa.mp4      # video completo
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

TEXT = {
    "es": {
        "c1": ["En 1963, Edward Lorenz redujo la convección", "de la atmósfera a tres ecuaciones."],
        "c2": ["3.000 atmósferas simuladas a la vez,", "cada una de un color."],
        "c2b": ["Diferencia inicial entre ellas:", "una millonésima."],
        "c3": ["Mismas ecuaciones.", "Casi el mismo punto de partida."],
        "c4": ["Y cada una termina", "en un futuro distinto."],
        "c5": ["Por eso el pronóstico del tiempo", "es una probabilidad, no una certeza."],
        "title": "EL EFECTO MARIPOSA",
        "hud": "SEPARACIÓN MÁXIMA ENTRE ATMÓSFERAS",
        "tag": "ATRACTOR DE LORENZ · 1963",
        "dec": ",",
    },
    "en": {
        "c1": ["In 1963, Edward Lorenz reduced atmospheric", "convection to three equations."],
        "c2": ["3,000 atmospheres simulated at once,", "each one a different color."],
        "c2b": ["Initial difference between them:", "one millionth."],
        "c3": ["Same equations.", "Almost the same starting point."],
        "c4": ["And each one ends up", "in a different future."],
        "c5": ["That is why a weather forecast", "is a probability, not a certainty."],
        "title": "THE BUTTERFLY EFFECT",
        "hud": "MAX SEPARATION BETWEEN ATMOSPHERES",
        "tag": "LORENZ ATTRACTOR · 1963",
        "dec": ".",
    },
}
EQUATIONS = ["dx/dt = σ (y − x)", "dy/dt = x (ρ − z) − y", "dz/dt = x y − β z"]


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
# Pre-simulacion de las cabezas: da el calendario y los datos del HUD
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


def presim(x0, nsteps):
    s = member_offsets()
    sim = Lorenz(x0[0] + EPS * s * DIRV[0], x0[1] + EPS * s * DIRV[1], x0[2] + EPS * s * DIRV[2])
    sel = np.unique(np.concatenate([np.arange(0, M, 15), [M - 1]]))
    diag = np.empty(nsteps + 1)
    cent = np.empty((nsteps + 1, 3))
    sub = np.empty((nsteps + 1, sel.size, 3))
    for i in range(nsteps + 1):
        dx = sim.x.max() - sim.x.min(); dy = sim.y.max() - sim.y.min(); dz = sim.z.max() - sim.z.min()
        diag[i] = math.sqrt(dx * dx + dy * dy + dz * dz)
        cent[i] = (sim.x.mean(), sim.y.mean(), sim.z.mean())
        sub[i, :, 0] = sim.x[sel]; sub[i, :, 1] = sim.y[sel]; sub[i, :, 2] = sim.z[sel]
        if i < nsteps:
            sim.step(M)
    return diag, cent, sub


def build_schedule(diag, fps, tail_s):
    q = 60.0 / fps
    FAST, SLOW, NORM = 15.0 * q, 4.0 * q, 10.0 * q
    TH1, TH2 = 0.6, 20.0
    EXTRA = 700          # pasos en camara lenta despues de que la nube se abre

    def ramp(a, b, sec):
        n = max(1, int(round(sec * fps)))
        return [a + (b - a) * smooth((i + 1) / n) for i in range(n)]

    # La separacion oscila mucho a lo largo de la orbita: se usa el ultimo paso
    # en que estuvo por debajo del umbral, no el primero en que lo supera.
    n1 = int(np.nonzero(diag < TH1)[0][-1]) + 1
    n2 = int(np.nonzero(diag < TH2)[0][-1]) + 1 + EXTRA
    rates = [0.0] * int(round(2.6 * fps))
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


def camera_keys(tg, ts, tf, te):
    #        t                 az     el   dist   apertura
    return [
        (0.0,                [-100.0, 6.0, 120.0, 9.0]),
        (tg,                 [-99.0, 6.0, 116.0, 9.0]),
        (ts,                 [-84.0, 9.0, 104.0, 12.0]),
        ((ts + tf) * 0.5,    [-74.0, 12.0, 84.0, 20.0]),
        (tf,                 [-64.0, 14.0, 86.0, 18.0]),
        (tf + 3.5,           [-78.0, 26.0, 98.0, 13.0]),
        (te - 3.5,           [-102.0, 16.0, 106.0, 11.0]),
        (te,                 [-92.0, 8.0, 118.0, 9.0]),
    ]


def camera_basis(az, el, dist):
    a, e = math.radians(az), math.radians(el)
    C = TARGET + dist * np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
    F = TARGET - C
    F /= np.linalg.norm(F)
    R = np.cross(F, np.array([0.0, 0.0, 1.0]))
    R /= np.linalg.norm(R)
    U = np.cross(R, F)
    return C, R, U, F


# ----------------------------------------------------------------------------
# Texto
# ----------------------------------------------------------------------------
def load_font(names, size):
    from PIL import ImageFont
    base = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    for nme in names:
        p = os.path.join(base, nme)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


def make_layer(lines, font, color=(255, 255, 255), tracking=0.0, gap=0.06, shadow=0.8, maxw=None):
    """Devuelve (rgb premultiplicado, alfa) con una sombra suave para legibilidad."""
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
        x = (Wd - width(l)) * 0.5
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
    return rgb, alpha


def comp(frame, layer, cx, cy, a, anchor="c"):
    if a <= 0.001:
        return
    rgb, alpha = layer
    h, w = alpha.shape
    x0 = int(round(cx - w / 2)) if anchor == "c" else int(round(cx))
    y0 = int(round(cy - h / 2))
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

    def __init__(self, W, Hh, scale):
        self.W, self.Hh, self.scale = W, Hh, scale
        self.prev = None
        self.ref = None
        yy, xx = np.mgrid[0:Hh, 0:W].astype(np.float32)
        r = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - Hh / 2) / (Hh / 2)) ** 2)
        self.vig = np.clip(1.0 - 0.20 * r ** 2.4, 0.0, 1.0)[..., None].astype(np.float32)
        g = np.exp(-(r * 1.1) ** 2)[..., None]
        self.bg = (np.array([0.010, 0.011, 0.024], np.float32)
                   + g * np.array([0.022, 0.028, 0.060], np.float32)).astype(np.float32)
        rng = np.random.default_rng(99)
        self.dither = [((rng.random((Hh, W, 3), dtype=np.float32) - 0.5) / 255.0) for _ in range(4)]
        self.i = 0

    def reset(self):
        self.prev = None

    def __call__(self, acc, persist, expo):
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
        g = expo / self.ref
        l = lum * g
        Lm = np.log1p(self.K * l) / math.log1p(self.K * self.WP)
        np.clip(Lm, 0.0, 1.15, out=Lm)
        ratio = Lm / np.maximum(lum, 1e-20)
        col = hdr * ratio[..., None]
        col += (col - col.mean(axis=2, keepdims=True)) * self.SAT
        np.maximum(col, 0.0, out=col)
        d = np.clip((Lm - 0.58) / 0.55, 0.0, 1.0)
        d *= d
        col += (Lm[..., None] - col) * d[..., None]
        small = cv2.resize(col, (W // 4, Hh // 4), interpolation=cv2.INTER_AREA)
        b1 = cv2.GaussianBlur(small, (0, 0), 2.2 * self.scale + 0.3)
        b2 = cv2.GaussianBlur(small, (0, 0), 10.0 * self.scale + 0.5)
        bloom = cv2.resize(0.45 * b1 + 0.55 * b2, (W, Hh), interpolation=cv2.INTER_LINEAR)
        col += bloom
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


def fmt_sep(v, dec):
    return f"{v:.6f}".replace(".", dec)


# ----------------------------------------------------------------------------
# Principal
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="out/efecto_mariposa.mp4")
    ap.add_argument("--scale", type=float, default=1.0, help="1.0 = 1080x1080")
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--stills", default="", help="segundos separados por coma; guarda PNG en vez de video")
    ap.add_argument("--lang", default="es", choices=list(TEXT))
    ap.add_argument("--tail", type=float, default=10.0, help="segundos del acto final")
    ap.add_argument("--crf", type=int, default=16)
    ap.add_argument("--trail", type=int, default=100, help="longitud de estela (pasos) en el acto final")
    ap.add_argument("--info", action="store_true", help="solo muestra el calendario")
    args = ap.parse_args()
    lowprio()
    import cv2
    cv2.setNumThreads(4)

    scale = args.scale
    W = Hh = int(round(1080 * scale / 2) * 2)
    fps = args.fps
    T = TEXT[args.lang]

    t0 = time.time()
    x0 = find_start()
    diag, cent, sub = presim(x0, 26000)
    nsub, ci, ev = build_schedule(diag, fps, args.tail)
    NF = len(nsub)
    tg, ts, tf, te, tb = (ev[k] / fps for k in ("go", "slow", "fast", "end", "burst"))
    print(f"inicio {x0.round(3)}  presim {time.time() - t0:.1f}s")
    print(f"fotogramas {NF} ({te:.1f}s)  go {tg:.2f}s  slow {ts:.2f}s  burst {tb:.2f}s  fast {tf:.2f}s  pasos {ci[-1]}")
    if args.info:
        for sec in range(0, int(te) + 1):
            f = min(NF - 1, sec * fps)
            print(f"  t={sec:2d}s paso={ci[f]:6d} sep={diag[ci[f]]:.6f}")
        return

    keys = camera_keys(tg, ts, tf, te)
    focal = (Hh / 2) / math.tan(math.radians(FOV_DEG / 2))

    stills = [float(v) for v in args.stills.split(",") if v.strip()]
    if stills:
        targets = sorted({min(NF - 1, int(round(v * fps))) for v in stills})
        render_set = set()
        for f in targets:
            render_set.update(range(max(0, f - 5), f + 1))
        last = max(targets)
    else:
        targets = None
        render_set = None
        last = NF - 1

    # --- capas de texto ---
    def px(v):
        return max(8, int(round(v * scale)))

    def trk(v):
        return v * scale

    f_cap = load_font(["segoeui.ttf", "arial.ttf"], px(40))
    f_eq = load_font(["cambriai.ttf", "timesi.ttf"], px(40))
    f_title = load_font(["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf"], px(58))
    f_lab = load_font(["seguisb.ttf", "segoeui.ttf", "arial.ttf"], px(17))
    f_num = load_font(["consola.ttf", "cour.ttf"], px(42))
    cap_y = 0.865 * Hh
    half = (ts - tg - 1.7) * 0.5
    caps = [
        (make_layer(T["c1"], f_cap), W / 2, 0.135 * Hh, 0.3, tg + 0.7),
        (make_layer(EQUATIONS, f_eq, color=(190, 215, 255), gap=0.12), W / 2, 0.30 * Hh, 0.8, tg + 0.7),
        (make_layer(T["c2"], f_cap), W / 2, cap_y, tg + 1.7, tg + 1.6 + half),
        (make_layer(T["c2b"], f_cap), W / 2, cap_y, tg + 1.8 + half, ts - 0.1),
        (make_layer(T["c3"], f_cap), W / 2, cap_y, ts + 0.1, tb - 0.2),
        (make_layer(T["c4"], f_cap), W / 2, cap_y, tb + 0.3, tf + 2.4),
        (make_layer(T["c5"], f_cap), W / 2, cap_y, tf + 3.0, te + 1.0),
        (make_layer([T["title"]], f_title, tracking=trk(9)), W / 2, 0.115 * Hh, te - 4.6, te + 1.0),
    ]
    hud_lab = make_layer([T["hud"]], f_lab, color=(165, 185, 220), tracking=trk(2.2), shadow=0.6)
    tag = make_layer([T["tag"]], f_lab, color=(140, 158, 190), tracking=trk(2.2), shadow=0.6)

    # --- trabajadores ---
    nw = args.workers
    shms, conns, procs = [], [], []
    for w in range(nw):
        shm = shared_memory.SharedMemory(create=True, size=Hh * W * 3 * 4)
        a, b = Pipe()
        p = Process(target=worker_main, args=(b, shm.name, w, nw, W, Hh, scale, x0, 1234), daemon=True)
        p.start()
        shms.append(shm); conns.append(a); procs.append(p)
    bufs = [np.ndarray((Hh, W, 3), dtype=np.float32, buffer=s.buf) for s in shms]
    for c in conns:
        c.recv()
    print(f"trabajadores listos: {nw} x {M * S // nw} particulas  ({time.time() - t0:.1f}s)")
    sys.stdout.flush()

    ff = None
    out = args.out
    os.makedirs(os.path.dirname(os.path.abspath(out)) or ".", exist_ok=True)
    if not stills:
        import imageio_ffmpeg
        log = open(os.path.splitext(out)[0] + ".ffmpeg.log", "wb")
        ff = subprocess.Popen([
            imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
            "-s", f"{W}x{Hh}", "-r", str(fps), "-i", "-", "-an",
            "-vf", "scale=out_color_matrix=bt709:out_range=tv",
            "-c:v", "libx264", "-preset", "slow", "-crf", str(args.crf), "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-movflags", "+faststart", out], stdin=subprocess.PIPE, stdout=log, stderr=log)

    dev = Developer(W, Hh, scale)
    acc = np.empty((Hh, W, 3), dtype=np.float32)
    prev_rendered = -10
    tr0 = time.time()
    done = 0
    for f in range(last + 1):
        t = f / fps
        render = render_set is None or f in render_set
        az, el, dist, apv = hermite(keys, t)
        C, R, U, F = camera_basis(az, el, dist)
        step_now = int(ci[f])
        zhead = float(np.dot(cent[step_now] - C, F))
        fb = smooth((t - ts) / max(1e-6, (tf - ts)))
        focus = zhead * (1 - fb) + dist * fb
        # Tras abrirse la nube, las estelas se acortan: cada atmosfera pasa a ser
        # un cometa corto y los colores dejan de mezclarse en blanco.
        jv = int(round(JMAX + (args.trail - JMAX) * smooth((t - tb - 1.2) / max(1e-6, tf + 2.2 - tb - 1.2))))
        cam = {"C": C, "R": R, "U": U, "F": F, "f": focal, "cx": W / 2, "cy": Hh / 2,
               "focus": focus, "ap": apv, "jvis": jv}
        n_before = int(ci[f - 1]) if f > 0 else 0
        msg = (int(nsub[f]), n_before, render, cam)
        for c in conns:
            c.send(msg)
        for c in conns:
            c.recv()
        if not render:
            continue
        if f != prev_rendered + 1:
            dev.reset()
        prev_rendered = f
        np.copyto(acc, bufs[0])
        for b in bufs[1:]:
            acc += b
        persist = 0.30 + 0.25 * smooth((t - ts) / max(1e-6, tf - ts))
        col = dev(acc.copy(), persist, 1.0)

        # texto
        for layer, cx, cy, a0, a1 in caps:
            a = smooth((t - a0) / 0.5) * smooth((a1 - t) / 0.5)
            comp(col, layer, cx, cy, a)
        ah = smooth((t - tg - 0.7) / 0.8) * smooth((te - 4.9 - t) / 0.7) * 0.95
        if ah > 0:
            mx = 54 * scale
            comp(col, hud_lab, mx - hud_lab[1].shape[0] * 0.0 - px(12), 62 * scale, ah, anchor="l")
            sp = sub[step_now]
            dd = sp[:, None, :] - sp[None, :, :]
            sep = float(np.sqrt((dd * dd).sum(axis=2).max()))
            num = make_layer([fmt_sep(sep, T["dec"])], f_num, shadow=0.6)
            comp(col, num, mx - px(29), 108 * scale, ah, anchor="l")
            bx0, bx1 = int(mx + px(2)), int(mx + 330 * scale)
            by = int(150 * scale)
            bh = max(2, int(round(3 * scale)))
            frac = min(1.0, max(0.0, (math.log10(max(sep, 1e-12)) + 6.0) / 8.0))
            col[by:by + bh, bx0:bx1] += 0.16 * ah
            col[by:by + bh, bx0:bx0 + int((bx1 - bx0) * frac)] += 0.8 * ah
            comp(col, tag, W - 54 * scale - tag[1].shape[1] + px(12), 62 * scale, ah * 0.9, anchor="l")
        fade = smooth(t / 0.35) * smooth((te - t) / 0.45 + 0.02)
        img = dev.finish(col, fade)

        if stills:
            if f in targets:
                name = os.path.join(os.path.dirname(os.path.abspath(out)), f"still_{t:05.2f}s.png")
                cv2.imwrite(name, cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
                print("guardado", name)
        else:
            ff.stdin.write(img.tobytes())
        done += 1
        if done % 30 == 0 or f == last:
            el_s = time.time() - tr0
            total = len(render_set) if render_set is not None else NF
            print(f"fotograma {f + 1}/{last + 1}  {el_s / (f + 1):.2f}s/f  faltan {el_s / (f + 1) * (last - f) / 60:.1f} min")
        sys.stdout.flush()

    for c in conns:
        c.send(None)
    for p in procs:
        p.join(timeout=10)
    for s in shms:
        s.close()
        s.unlink()
    if ff is not None:
        ff.stdin.close()
        ff.wait()
        print("video:", out, f"{os.path.getsize(out) / 1e6:.1f} MB")
    print(f"total {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
