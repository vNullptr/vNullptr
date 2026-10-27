#!/usr/bin/env python3
"""Render the profile cards in assets/ from profile.toml.

Stdlib only (Python 3.11+). Every card is emitted twice, -dark.svg and
-light.svg, and the README picks one with <picture> + prefers-color-scheme.

    python scripts/render.py            # render everything
    LOG_FIXTURE=x.json python ...       # render the log from a local fixture

In CI, GITHUB_TOKEN is used to pull recent commits for the log card. If the
fetch fails, the previous log card is left untouched.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import random
import re
import subprocess
import sys
import tomllib
import urllib.request
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
ASSETS = ROOT / "assets"

FONT = ("ui-monospace,SFMono-Regular,'SF Mono',Menlo,Consolas,"
        "'Liberation Mono','DejaVu Sans Mono',monospace")
CW = 0.6  # monospace advance width as a fraction of font-size

THEMES = {
    "dark": dict(fg="#e6edf3", muted="#9198a1", dim="#656c76",
                 line="#30363d", faint="#21262d", bg="#0d1117"),
    "light": dict(fg="#1f2328", muted="#59636e", dim="#8c959f",
                  line="#d1d9e0", faint="#eaeef2", bg="#ffffff"),
}


# ── primitives ──────────────────────────────────────────────────────────

def esc(s) -> str:
    return escape(str(s), {'"': "&quot;"})


def f(n: float) -> str:
    """Compact number formatting for attributes."""
    s = f"{n:.2f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def css(t: dict) -> str:
    return (
        f"text{{font-family:{FONT}}}"
        f".fg{{fill:{t['fg']}}}.mu{{fill:{t['muted']}}}.dm{{fill:{t['dim']}}}"
        ".b{font-weight:600}"
        ".in{animation:in .7s cubic-bezier(.2,.7,.2,1) both}"
        "@keyframes in{from{opacity:0;transform:translateY(4px)}"
        "to{opacity:1;transform:translateY(0)}}"
        ".blink{animation:blink 1.1s steps(1) infinite}"
        "@keyframes blink{50%{opacity:0}}"
        ".pulse{animation:pulse 2.4s ease-in-out infinite}"
        "@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}"
        ".draw{stroke-dasharray:1;animation:draw 1.4s cubic-bezier(.4,0,.2,1) both}"
        "@keyframes draw{from{stroke-dashoffset:1}to{stroke-dashoffset:0}}"
        ".shimmer{animation:shimmer 1.8s ease-in-out infinite}"
        "@keyframes shimmer{0%,100%{opacity:.35}50%{opacity:.8}}"
        "@media (prefers-reduced-motion:reduce){"
        ".in,.draw,.blink,.pulse,.shimmer{animation:none}}"
    )


def doc(w: int, h: int, t: dict, label: str, body: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
        f'viewBox="0 0 {w} {h}" role="img" aria-label="{esc(label)}" '
        f'xml:space="preserve" fill="none">'
        f"<title>{esc(label)}</title><style>{css(t)}</style>{body}</svg>\n"
    )


def frame(w: int, h: int, t: dict) -> str:
    return (f'<rect x=".5" y=".5" width="{w - 1}" height="{h - 1}" rx="10" '
            f'stroke="{t["line"]}"/>')


def text(x, y, s, fs=13, cls="fg", anchor=None, delay=None, extra="") -> str:
    a = f' text-anchor="{anchor}"' if anchor else ""
    c = cls + (" in" if delay is not None else "")
    st = f' style="animation-delay:{f(delay)}s"' if delay is not None else ""
    return (f'<text x="{f(x)}" y="{f(y)}" font-size="{f(fs)}" class="{c}"'
            f'{a}{st}{extra}>{s}</text>')


def tspan(s, cls) -> str:
    return f'<tspan class="{cls}">{esc(s)}</tspan>'


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def timeline(points: list[tuple[float, object]], period: float) -> tuple[str, str]:
    """[(t, value), ...] -> (values, keyTimes) for a looping SMIL animate."""
    vals = ";".join(f(v) if isinstance(v, (int, float)) else str(v) for _, v in points)
    kts = ";".join(f"{min(max(tt / period, 0), 1):.4f}" for tt, _ in points)
    return vals, kts


# ── header ──────────────────────────────────────────────────────────────

def typed(uid, x, y, s, fs, begin, t, cps=13.0):
    """Text that types itself in, with a caret riding along. Returns (svg, end)."""
    n, cw = len(s), CW * fs
    dur = max(n / cps, 0.25)
    widths = ";".join(f(i * cw) for i in range(n + 1))
    xs = ";".join(f(x + i * cw) for i in range(n + 1))
    kts = ";".join(f"{i / n:.4f}" for i in range(n + 1))
    anim = f'calcMode="discrete" begin="{f(begin)}s" dur="{f(dur)}s" fill="freeze"'
    out = (
        f'<clipPath id="{uid}"><rect x="{f(x)}" y="{f(y - fs)}" height="{f(fs * 1.5)}" width="{f(n * cw + 2)}">'
        f'<set attributeName="width" to="0" begin="0s"/><animate attributeName="width" values="{widths}" keyTimes="{kts}" {anim}/></rect></clipPath>'
        f'<text x="{f(x)}" y="{f(y)}" font-size="{f(fs)}" class="fg" clip-path="url(#{uid})" '
        f'textLength="{f(n * cw)}" lengthAdjust="spacingAndGlyphs">{esc(s)}</text>'
        f'<rect x="{f(x)}" y="{f(y - fs + 1)}" width="{f(cw * .85)}" height="{f(fs + 3)}" '
        f'fill="{t["fg"]}" opacity="0">'
        f'<set attributeName="opacity" to=".8" begin="{f(begin - .35)}s"/>'
        f'<animate attributeName="x" values="{xs}" keyTimes="{kts}" {anim}/>'
        f'<set attributeName="opacity" to="0" begin="{f(begin + dur + .3)}s"/></rect>'
    )
    return out, begin + dur


def tokenize(motto: str) -> list[str]:
    return re.findall(r"\w+|[^\w\s]", motto)[:6]


def gen_weights(tokens: list[str]) -> list[list[float]]:
    rng = random.Random(" ".join(tokens))
    rows = []
    for i in range(len(tokens)):
        raw = [rng.random() ** 2 + (.5 if j == i else 0) for j in range(i + 1)]
        s = sum(raw)
        rows.append([r / s for r in raw])
    return rows


def render_header(cfg: dict, t: dict) -> str:
    W, H = 840, 252
    hd = cfg["header"]
    fs, x0 = 14, 28
    cw = CW * fs
    p = [frame(W, H, t)]

    # window chrome
    for i in range(3):
        p.append(f'<circle cx="{22 + i * 14}" cy="21" r="4.5" stroke="{t["dim"]}"/>')
    p.append(text(W / 2, 25, esc(hd["title"]), 11, "dm", "middle"))
    p.append(f'<path d="M1 40.5H{W - 1}" stroke="{t["faint"]}"/>')

    # terminal: whoami
    def prompt(y, at):
        return text(x0, y, "~ $", fs, "dm", delay=at,
                    extra=f' textLength="{f(3 * cw)}" lengthAdjust="spacingAndGlyphs"')

    y, b = 74, .7
    p.append(prompt(y, b - .45))
    s, end = typed("hd-c1", x0 + 4 * cw, y, "whoami", fs, b, t)
    p.append(s)
    at = end + .3
    y += 24
    p.append(text(x0, y, tspan(hd["name"], "fg b") + tspan(f"  aka {hd['alias']}", "dm"),
                  fs, "fg", delay=at))
    for i, line in enumerate(hd.get("lines", [])):
        y += 22
        p.append(text(x0, y, esc(line), fs, "mu", delay=at + .12 * (i + 1)))

    # terminal: motto
    y += 34
    b2 = at + .9
    p.append(prompt(y, b2 - .45))
    s, end = typed("hd-c2", x0 + 4 * cw, y, "cat motto.txt", fs, b2, t)
    p.append(s)
    y += 22
    p.append(text(x0, y, esc(hd["motto"]), fs, "fg", delay=end + .3))

    # final prompt with blinking cursor
    y += 34
    at = end + .9
    p.append(prompt(y, at))
    p.append(f'<g opacity="0"><set attributeName="opacity" to="1" begin="{f(at)}s"/>'
             f'<rect class="blink" x="{f(x0 + 4 * cw)}" y="{f(y - fs + 1)}" '
             f'width="{f(cw * .85)}" height="{f(fs + 3)}" fill="{t["fg"]}" opacity=".8"/></g>')

    # divider
    p.append(f'<path d="M520.5 58V{H - 18}" stroke="{t["faint"]}"/>')

    # causal self-attention over the motto, decoded one query row at a time
    toks = tokenize(hd["motto"])
    n = len(toks)
    wts = hd.get("attention") or gen_weights(toks)
    if len(wts) < n or any(len(wts[i]) != i + 1 for i in range(n)):
        wts = gen_weights(toks)
    wts = [[v / sum(r) for v in r] for r in wts[:n]]

    px, top, cell, step = 540, 92, 20, 24
    mx = 604
    p.append(text(px, 72, "self-attention · causal · head 0", 10.5, "dm"))

    row_dt, lead, tail = 1.1, .8, 1.8
    T = lead + n * row_dt + tail
    rs = [lead + i * row_dt for i in range(n)]  # row start times

    # sliding query highlight
    ys = [(0, top - 4)] + [(rs[i], top - 4 + i * step) for i in range(n)] + [(T, top - 4 + (n - 1) * step)]
    ws = [(0, step)] + [(rs[i], (i + 1) * step + 4) for i in range(n)] + [(T, n * step + 4)]
    vy, ky = timeline(ys, T)
    vw, kw = timeline(ws, T)
    vo, ko = timeline([(0, 0), (rs[0] - .01, 0), (rs[0], .5), (rs[-1] + row_dt, .5),
                       (rs[-1] + row_dt + .2, 0), (T, 0)], T)
    p.append(
        f'<rect x="{mx - 4}" rx="5" height="{step + 4}" stroke="{t["fg"]}" opacity="0">'
        f'<animate attributeName="y" values="{vy}" keyTimes="{ky}" calcMode="discrete" dur="{f(T)}s" repeatCount="indefinite"/>'
        f'<animate attributeName="width" values="{vw}" keyTimes="{kw}" calcMode="discrete" dur="{f(T)}s" repeatCount="indefinite"/>'
        f'<animate attributeName="opacity" values="{vo}" keyTimes="{ko}" dur="{f(T)}s" repeatCount="indefinite"/></rect>'
    )

    fade_out = T - .9
    for i, tok in enumerate(toks):
        cy = top + i * step
        a = rs[i]
        # query label
        v, k = timeline([(0, .4), (a, .4), (a + .15, 1), (a + row_dt, 1), (a + row_dt + .2, .65),
                         (fade_out, .65), (fade_out + .5, .4), (T, .4)], T)
        p.append(f'<text x="{mx - 12}" y="{cy + 14}" font-size="11" class="fg" text-anchor="end" opacity=".4">'
                 f'{esc(tok)}<animate attributeName="opacity" values="{v}" keyTimes="{k}" '
                 f'dur="{f(T)}s" repeatCount="indefinite"/></text>')
        for j in range(n):
            cx = mx + j * step
            if j > i:  # masked future positions
                p.append(f'<circle cx="{cx + cell / 2}" cy="{cy + cell / 2}" r="1.2" fill="{t["dim"]}" opacity=".6"/>')
                continue
            w = wts[i][j]
            base, peak, keep = .07, .18 + .82 * w, .1 + .45 * w
            v, k = timeline([(0, base), (a, base), (a + .18, peak), (a + .85, peak),
                             (a + row_dt, keep), (fade_out, keep), (fade_out + .5, base), (T, base)], T)
            p.append(f'<rect x="{cx}" y="{cy}" width="{cell}" height="{cell}" rx="3" fill="{t["fg"]}" '
                     f'opacity="{f(base)}"><animate attributeName="opacity" values="{v}" keyTimes="{k}" '
                     f'dur="{f(T)}s" repeatCount="indefinite"/></rect>')
        # which token this query attends to most
        top_j = max(range(i + 1), key=lambda j: wts[i][j])
        v, k = timeline([(0, 0), (a + .1, 0), (a + .3, 1), (a + row_dt - .05, 1), (a + row_dt + .1, 0), (T, 0)], T)
        p.append(f'<text x="{mx + n * step + 6}" y="{cy + 14}" font-size="11" class="mu" opacity="0">'
                 f'→ {esc(toks[top_j])}<animate attributeName="opacity" values="{v}" keyTimes="{k}" '
                 f'dur="{f(T)}s" repeatCount="indefinite"/></text>')

    label = f"{hd['name']} ({hd['alias']}). " + ". ".join(hd.get("lines", [])) + f". {hd['motto']}"
    return doc(W, H, t, label, "".join(p))


# ── now ─────────────────────────────────────────────────────────────────

def updated_date() -> str:
    try:
        out = subprocess.run(["git", "log", "-1", "--format=%cs", "--", "profile.toml"],
                             cwd=ROOT, capture_output=True, text=True, timeout=10).stdout.strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", out):
            return out
    except Exception:
        pass
    return dt.date.today().isoformat()


def status_dot(cx, cy, state, t) -> str:
    if state == "active":
        return f'<circle class="pulse" cx="{cx}" cy="{cy}" r="4" fill="{t["fg"]}"/>'
    if state == "open":
        return (f'<circle cx="{cx}" cy="{cy}" r="4" stroke="{t["fg"]}" stroke-width="1.5"/>'
                f'<circle cx="{cx}" cy="{cy}" r="4" stroke="{t["fg"]}">'
                f'<animate attributeName="r" values="4;11" dur="2.4s" repeatCount="indefinite"/>'
                f'<animate attributeName="opacity" values=".7;0" dur="2.4s" repeatCount="indefinite"/></circle>')
    return f'<circle cx="{cx}" cy="{cy}" r="4" stroke="{t["dim"]}" stroke-width="1.5"/>'


def render_now(cfg: dict, t: dict) -> str:
    W = 840
    items = cfg.get("now", [])
    x_lab, x_main, x_end = 46, 150, 816
    p = []
    p.append(text(24, 32, tspan("◦ ", "dm") + tspan("now", "fg b"), 13, "fg"))
    p.append(text(x_end, 32, esc(f"updated {updated_date()}"), 11, "dm", "end"))
    p.append(f'<path d="M1 48.5H{W - 1}" stroke="{t["faint"]}"/>')

    y = 48
    for idx, it in enumerate(items):
        d = .15 + idx * .15
        has_track = bool(it.get("track"))
        hgt = 118 if has_track else 66
        if idx:
            p.append(f'<path d="M24 {y + .5}H{x_end}" stroke="{t["faint"]}" stroke-dasharray="2 4"/>')
        ty = y + 32
        p.append(f'<g class="in" style="animation-delay:{f(d)}s">'
                 + status_dot(30, ty - 4.5, it.get("state", "idle"), t)
                 + text(x_lab, ty, esc(it["label"]), 11.5, "dm")
                 + text(x_main, ty, esc(it["title"]), 15, "fg b")
                 + (text(x_end, ty, esc(it["meta"]), 11, "dm", "end") if it.get("meta") else "")
                 + (text(x_main, ty + 21, esc(it["detail"]), 12, "mu") if it.get("detail") else "")
                 + "</g>")
        if has_track:
            p.append(phase_track(it["track"], float(it.get("at", 0)), x_main, x_end, y + 80, t, d + .3))
        y += hgt

    H = y + 8
    label = "now: " + "; ".join(f"{i['label']} {i['title']}" for i in items)
    return doc(W, H, t, label, frame(W, H, t) + "".join(p))


def phase_track(phases, at, x0, x1, y, t, delay) -> str:
    n, gap = len(phases), 8
    seg = (x1 - x0 - gap * (n - 1)) / n
    out = []
    cur = min(int(at), n - 1)
    for i, name in enumerate(phases):
        sx = x0 + i * (seg + gap)
        out.append(f'<path d="M{f(sx)} {y}H{f(sx + seg)}" stroke="{t["faint"]}" stroke-width="4" stroke-linecap="round"/>')
        fill = 1.0 if i < cur else (at - cur if i == cur else 0)
        fill = max(min(fill, 1), 0)
        if fill > 0:
            L = max(seg * fill, 4)
            out.append(f'<path class="draw" style="animation-delay:{f(delay + i * .25)}s" d="M{f(sx)} {y}H{f(sx + L)}" '
                       f'stroke="{t["fg"]}" stroke-width="4" stroke-linecap="round" pathLength="1"/>')
        cls = "fg" if i == cur else ("mu" if i < cur else "dm")
        mark = "▸ " if i == cur else ""
        out.append(text(sx, y + 22, esc(mark + name), 11, cls, delay=delay + .1 + i * .08))
    # pulsing head at the current position
    sx = x0 + cur * (seg + gap) + seg * max(min(at - cur, 1), 0)
    out.append(f'<g opacity="0"><set attributeName="opacity" to="1" begin="{f(delay + 1.2)}s"/>'
               f'<circle cx="{f(sx)}" cy="{y}" r="4" fill="{t["fg"]}"/>'
               f'<circle cx="{f(sx)}" cy="{y}" r="4" stroke="{t["fg"]}">'
               f'<animate attributeName="r" values="4;12" dur="2s" repeatCount="indefinite"/>'
               f'<animate attributeName="opacity" values=".6;0" dur="2s" repeatCount="indefinite"/></circle></g>')
    return "".join(out)


# ── project cards ───────────────────────────────────────────────────────

def render_project(cfg: dict, pr: dict, t: dict) -> str:
    W, H = 412, 204
    user = cfg["user"].lower()
    p = [frame(W, H, t)]
    p.append(text(22, 32, esc(f"{user} / {pr['repo']}"), 11, "dm"))

    # status pill
    st = pr.get("status", "")
    if st:
        pw = len(st) * CW * 11 + 30
        px = W - 22 - pw
        live = st.lower() not in ("paused", "archived")
        dot = (f'<circle cx="{f(px + 12)}" cy="27.5" r="3" fill="{t["fg"]}"/>' if live else
               f'<circle cx="{f(px + 12)}" cy="27.5" r="3" stroke="{t["dim"]}" stroke-width="1.3"/>')
        p.append(f'<rect x="{f(px)}" y="17.5" width="{f(pw)}" height="20" rx="10" stroke="{t["line"]}"/>' + dot)
        p.append(text(px + 21, 31.5, esc(st), 11, "mu"))

    p.append(text(22, 62, esc(pr["title"]), 18, "fg b", delay=.1))
    for i, line in enumerate(pr.get("desc", [])[:2]):
        p.append(text(22, 86 + i * 18, esc(line), 12, "mu", delay=.2 + i * .08))

    # data flow with a packet travelling through it
    flow = pr.get("flow", [])
    if flow:
        fy, fh, fs = 132, 26, 11
        widths = [len(s) * CW * fs + 18 for s in flow]
        span = W - 44
        gap = (span - sum(widths)) / max(len(flow) - 1, 1)
        xs, x = [], 22.0
        for w in widths:
            xs.append(x)
            x += w + gap
        T = 3.6
        seg_t = (T - .6) / max(len(flow) - 1, 1)
        for i in range(len(flow) - 1):
            a, b = xs[i] + widths[i], xs[i + 1]
            cy = fy + fh / 2
            p.append(f'<path d="M{f(a + 4)} {f(cy)}H{f(b - 4)}" stroke="{t["line"]}" stroke-dasharray="2 3"/>')
            s0, s1 = i * seg_t, i * seg_t + seg_t * .8
            vx, kx = timeline([(0, a + 4), (s0, a + 4), (s1, b - 4), (T, b - 4)], T)
            vo, ko = timeline([(0, 0), (max(s0 - .01, 0), 0), (s0 + .05, 1), (s1, 1), (s1 + .08, 0), (T, 0)], T)
            p.append(f'<circle cy="{f(cy)}" r="2.5" fill="{t["fg"]}" opacity="0" cx="{f(a + 4)}">'
                     f'<animate attributeName="cx" values="{vx}" keyTimes="{kx}" dur="{T}s" repeatCount="indefinite"/>'
                     f'<animate attributeName="opacity" values="{vo}" keyTimes="{ko}" dur="{T}s" repeatCount="indefinite"/></circle>')
        for i, (lab, x, w) in enumerate(zip(flow, xs, widths)):
            hit = i * seg_t if i == 0 else (i - 1) * seg_t + seg_t * .8
            vs, ks = timeline([(0, t["line"]), (hit, t["fg"]), (min(hit + .45, T), t["line"]), (T, t["line"])], T)
            p.append(f'<rect x="{f(x)}" y="{fy}" width="{f(w)}" height="{fh}" rx="6" stroke="{t["line"]}">'
                     f'<animate attributeName="stroke" values="{vs}" keyTimes="{ks}" calcMode="discrete" '
                     f'dur="{T}s" repeatCount="indefinite"/></rect>')
            p.append(text(x + w / 2, fy + 17, esc(lab), fs, "fg", "middle"))

    tags = pr.get("tags", [])
    if tags:
        p.append(text(22, H - 22, esc("  ·  ".join(tags)), 11, "dm"))

    label = f"{pr['title']}: " + " ".join(pr.get("desc", [])) + (f" Status: {st}." if st else "")
    return doc(W, H, t, label, "".join(p))


# ── stack ───────────────────────────────────────────────────────────────

def render_stack(cfg: dict, t: dict) -> str:
    W = 840
    stack = cfg.get("stack", {})
    notes = cfg.get("stack_notes", {})
    fs, lh = 13, 23
    keyw = max((len(k) for k in stack), default=4)
    p = []
    # file tab
    p.append(f'<path d="M1 40.5H16.5M119.5 40.5H{W - 1}" stroke="{t["faint"]}"/>')
    p.append(f'<path d="M16.5 40.5V14.5a4 4 0 0 1 4-4h95a4 4 0 0 1 4 4v26" stroke="{t["line"]}"/>')
    p.append(text(68, 30, "stack.toml", 11.5, "fg", "middle"))
    p.append(text(W - 24, 30, "toml · utf-8", 11, "dm", "end"))

    lines = [tspan("[stack]", "mu")]
    for k, vals in stack.items():
        parts = [tspan(k.ljust(keyw), "fg"), tspan(" = [", "dm")]
        for i, v in enumerate(vals):
            if i:
                parts.append(tspan(", ", "dm"))
            parts.append(tspan(f'"{v}"', "mu"))
        parts.append(tspan("]", "dm"))
        if k in notes:
            parts.append(tspan(f"  # {notes[k]}", "dm"))
        lines.append("".join(parts))

    y = 70
    for i, ln in enumerate(lines):
        p.append(text(40, y, str(i + 1), 11, "dm", "end"))
        p.append(text(56, y, ln, fs, "fg", delay=.05 + i * .07))
        y += lh
    H = y - lh + 26
    label = "stack: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in stack.items())
    return doc(W, H, t, label, frame(W, H, t) + "".join(p))


# ── log ─────────────────────────────────────────────────────────────────

def gh_get(url: str):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "vNullptr-profile-renderer",
        **({"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}"} if os.environ.get("GITHUB_TOKEN") else {}),
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def fetch_commits(cfg: dict) -> list[dict] | None:
    fx = os.environ.get("LOG_FIXTURE")
    if fx:
        return json.loads(Path(fx).read_text())
    user = cfg["user"]
    lcfg = cfg.get("log", {})
    skip = {s.lower() for s in lcfg.get("exclude", [])}
    try:
        repos = gh_get(f"https://api.github.com/users/{user}/repos?sort=pushed&per_page=20&type=owner")
        repos = [r for r in repos if not r.get("fork") and r["name"].lower() not in skip][:8]
        out = []
        for r in repos:
            try:
                cs = gh_get(f"https://api.github.com/repos/{r['full_name']}/commits?author={user}&per_page=5")
            except Exception:
                continue
            for c in cs:
                msg = c["commit"]["message"].splitlines()[0].strip()
                if not msg or msg.startswith("Merge "):
                    continue
                out.append({"repo": r["name"], "sha": c["sha"][:7], "msg": msg,
                            "date": c["commit"]["author"]["date"]})
        out.sort(key=lambda c: c["date"], reverse=True)
        return out
    except Exception as e:  # network / rate limit: keep the previous card
        print(f"log: fetch failed ({e})", file=sys.stderr)
        return None


def render_log(cfg: dict, commits: list[dict], t: dict) -> str:
    W = 840
    n = int(cfg.get("log", {}).get("count", 6))
    commits = commits[:n]
    p = []
    p.append(text(24, 32, tspan("~ $ ", "dm") + tspan("git log --all-repos --oneline", "fg")
                  + tspan(f" -n {n}", "mu"), 13, "fg"))
    p.append(text(W - 24, 32, "synced daily", 11, "dm", "end"))
    p.append(f'<path d="M1 48.5H{W - 1}" stroke="{t["faint"]}"/>')

    y0, lh = 78, 26
    if not commits:
        for i in range(4):
            y = y0 + i * lh
            p.append(f'<circle cx="30" cy="{y - 4}" r="3" stroke="{t["dim"]}"/>')
            for x, w in ((46, 56), (118, 130 - i * 12), (300, 300 - i * 40), (W - 74, 50)):
                p.append(f'<rect class="shimmer" style="animation-delay:{f(i * .15)}s" x="{x}" y="{y - 10}" '
                         f'width="{w}" height="10" rx="5" fill="{t["faint"]}"/>')
        y = y0 + 4 * lh
        p.append(text(W / 2, y + 2, "waiting for the first sync · the workflow fills this in", 11.5, "dm", "middle"))
        H = y + 26
        return doc(W, H, t, "recent commits: waiting for first sync", frame(W, H, t) + "".join(p))

    # git graph rail
    p.append(f'<path d="M30 {y0 - 4}V{y0 - 4 + (len(commits) - 1) * lh}" stroke="{t["line"]}"/>')
    for i, c in enumerate(commits):
        y = y0 + i * lh
        d = .1 + i * .08
        head = i == 0
        dot = (f'<circle cx="30" cy="{y - 4}" r="3.5" fill="{t["fg"]}"/>' if head else
               f'<circle cx="30" cy="{y - 4}" r="3" fill="{t["bg"]}" stroke="{t["muted"]}" stroke-width="1.3"/>')
        date = dt.datetime.fromisoformat(c["date"].replace("Z", "+00:00")).strftime("%b %d").lower()
        p.append(f'<g class="in" style="animation-delay:{f(d)}s">' + dot
                 + text(46, y, esc(c["sha"]), 12, "dm")
                 + text(118, y, esc(trunc(c["repo"], 22)), 12, "mu")
                 + text(300, y, esc(trunc(c["msg"], 58)), 12.5, "fg")
                 + text(W - 24, y, esc(date), 11, "dm", "end") + "</g>")
    H = y0 + (len(commits) - 1) * lh + 26
    label = "recent commits: " + "; ".join(f"{c['repo']}: {c['msg']}" for c in commits)
    return doc(W, H, t, label, frame(W, H, t) + "".join(p))


# ── main ────────────────────────────────────────────────────────────────

def main() -> int:
    cfg = tomllib.loads((ROOT / "profile.toml").read_text(encoding="utf-8"))
    ASSETS.mkdir(exist_ok=True)
    commits = fetch_commits(cfg)
    written = 0

    def write(name: str, svg: str):
        nonlocal written
        path = ASSETS / name
        if not path.exists() or path.read_text(encoding="utf-8") != svg:
            path.write_text(svg, encoding="utf-8")
            written += 1
            print(f"  wrote {path.relative_to(ROOT)}")

    for theme, t in THEMES.items():
        write(f"header-{theme}.svg", render_header(cfg, t))
        write(f"now-{theme}.svg", render_now(cfg, t))
        for pr in cfg.get("projects", []):
            write(f"project-{pr['repo']}-{theme}.svg", render_project(cfg, pr, t))
        write(f"stack-{theme}.svg", render_stack(cfg, t))
        log_path = ASSETS / f"log-{theme}.svg"
        if commits is not None or not log_path.exists():
            write(log_path.name, render_log(cfg, commits or [], t))

    print(f"done · {written} file(s) changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
