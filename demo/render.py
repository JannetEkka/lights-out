"""Render demo/demo.cast to demo/lights-out-demo.mp4 (1920x1080, under 90 s, captions burned in).

The terminal is replayed through pyte (a terminal emulator) and drawn with Pillow, frame by frame.
Waiting time (the model thinking) is compressed: no gap between two output events lasts more than
GAP_CAP seconds, and a scene that is still longer than its slot plays faster, with a "sped up ×N"
tag on screen. Nothing is added to the terminal itself; the results card is drawn from
bench/results.json.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H, FPS = 1920, 1080, 15
TERM_H = 900
GAP_CAP = 0.6
FONT = "/usr/share/fonts/truetype/jetbrains-mono/JetBrainsMono-{}.ttf"
TERM_FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono{}.ttf"   # wider glyph coverage for the terminal
BG, FG, PAPER, RED, GREEN, AMBER, DIM = (24, 23, 21), (225, 222, 214), (246, 243, 235), (214, 48, 48), (64, 178, 96), (214, 160, 40), (120, 116, 108)
PALETTE = {"black": (40, 40, 40), "red": RED, "green": GREEN, "brown": AMBER, "yellow": AMBER,
           "blue": (90, 140, 220), "magenta": (190, 110, 200), "cyan": (80, 180, 190), "white": FG,
           "default": FG}


def load_cast(path: Path):
    lines = path.read_text().splitlines()
    head = json.loads(lines[0])
    events = [json.loads(x) for x in lines[1:] if x.strip()]
    return head, events


def compress(events):
    """[(compressed_t, kind, data)] with gaps capped."""
    out, ct, prev = [], 0.0, 0.0
    for t, kind, data in events:
        ct += min(t - prev, GAP_CAP)
        prev = t
        out.append((ct, kind, data))
    return out


class Term:
    def __init__(self, cols, rows):
        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.cols, self.rows = cols, rows
        size = 26
        while True:
            f = ImageFont.truetype(TERM_FONT.format(""), size)
            cw = f.getlength("M")
            if cw * cols <= W - 80 and size * 1.0 * rows <= TERM_H - 40 or size <= 12:
                break
            size -= 1
        self.font = f
        self.bold = ImageFont.truetype(TERM_FONT.format("-Bold"), size)
        self.cw, self.ch = cw, int(size * 1.0) + 2
        self.ox = (W - cw * cols) / 2
        self.oy = 20

    def feed(self, data: str):
        self.stream.feed(data)

    def draw(self, d: ImageDraw.ImageDraw):
        for y in range(self.rows):
            row = self.screen.buffer[y]
            x = 0
            while x < self.cols:
                c = row[x]
                style = (c.fg, c.bg, c.bold, c.reverse)
                run = c.data
                x2 = x + 1
                while x2 < self.cols and (row[x2].fg, row[x2].bg, row[x2].bold, row[x2].reverse) == style:
                    run += row[x2].data
                    x2 += 1
                fg = _color(c.fg, FG)
                bg = _color(c.bg, None) if c.bg != "default" else None
                if c.reverse:
                    fg, bg = (bg or BG), fg
                px, py = self.ox + x * self.cw, self.oy + y * self.ch
                if bg:
                    d.rectangle((px, py, px + len(run) * self.cw, py + self.ch), fill=bg)
                if run.strip():
                    d.text((px, py), run, font=self.bold if c.bold else self.font, fill=fg)
                x = x2


def _color(name, default):
    if name in PALETTE:
        return PALETTE[name]
    if isinstance(name, str) and len(name) == 6:
        try:
            return tuple(int(name[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            pass
    return default


CAP = ImageFont.truetype(FONT.format("Bold"), 46)
SMALL = ImageFont.truetype(FONT.format("Regular"), 26)


def caption(d, text, speed):
    d.rectangle((0, TERM_H, W, H), fill=(14, 13, 12))
    if text:
        tw = d.textlength(text, font=CAP)
        d.text(((W - tw) / 2, TERM_H + 52), text, font=CAP, fill=PAPER)
    if speed > 1.05:
        tag = f"sped up ×{speed:.0f}" if speed >= 2 else f"sped up ×{speed:.1f}"
        d.text((W - d.textlength(tag, font=SMALL) - 30, TERM_H + 20), tag, font=SMALL, fill=DIM)


def card(lines, title):
    img = Image.new("RGB", (W, TERM_H), BG)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT.format("Regular"), 30)
    b = ImageFont.truetype(FONT.format("Bold"), 30)
    d.rectangle((160, 40, W - 160, TERM_H - 30), fill=PAPER)
    d.text((210, 80), title, font=b, fill=(30, 30, 28))
    y = 150
    for text, color, bold in lines:
        d.text((210, y), text, font=b if bold else f, fill=color)
        y += 46
    return img


def results_card():
    r = json.loads((HERE.parent / "bench" / "results.json").read_text())
    ink = (30, 30, 28)
    a, b, c = r.get("A", {}), r.get("B", {}), r.get("C", {})
    share = lambda x: "–" if x.get("leftovers_named_share") is None else f"{x['leftovers_named_share'] * 100:.0f}%"
    rows = [
        (f"{'10 tasks, fresh moto each, model: Nemotron 3 Ultra':<62}", (110, 106, 98), False),
        ("", ink, False),
        (f"{'':<34}{'alone':>9}{'+prompt':>10}{'+Lights Out':>14}", ink, True),
        (f"{'left running by accident':<34}{a.get('left_running_unintended', '–'):>9}{b.get('left_running_unintended', '–'):>10}{c.get('left_running_unintended', '–'):>14}", RED, False),
        (f"{'  $/month':<34}{'$' + format(a.get('left_running_usd_month', 0), ',.2f'):>9}{'$' + format(b.get('left_running_usd_month', 0), ',.2f'):>10}{'$' + format(c.get('left_running_usd_month', 0), ',.2f'):>14}", RED, False),
        (f"{'creates nobody approved':<34}{a.get('unapproved_creates', '–'):>9}{b.get('unapproved_creates', '–'):>10}{c.get('unapproved_creates', '–'):>14}", ink, False),
        (f"{'leftovers named by ID':<34}{share(a):>9}{share(b):>10}{share(c):>14}", ink, False),
        (f"{'wrong all-clean claims':<34}{a.get('wrong_all_clean_claims', '–'):>9}{b.get('wrong_all_clean_claims', '–'):>10}{c.get('wrong_all_clean_claims', '–'):>14}", ink, False),
        ("", ink, False),
        (f"{'after /lights out, still running':<34}{'':>9}{'':>10}{c.get('after_lights_out_unintended', '–'):>14}", GREEN, True),
        (f"{'keeps recorded (2 stay-up tasks)':<34}{'':>9}{'':>10}{c.get('keeps_recorded', '–'):>14}", ink, False),
    ]
    return card(rows, "Same model, same tasks: measured")


def end_card():
    ink = (30, 30, 28)
    return card([
        ("hermes plugins install lights-out", ink, True),
        ("", ink, False),
        ("github.com/JannetEkka/lights-out", ink, False),
        ("", ink, False),
        ("A plugin for Hermes Agent by @NousResearch", (110, 106, 98), False),
        ("AWS CLI, 7 resource types. Demo runs on moto (emulated AWS);", (110, 106, 98), False),
        ("prices: AWS us-east-1 on-demand, read 2026-11-10.", (110, 106, 98), False),
    ], "Lights Out for Hermes Agent")


def find_rows(term, first: str, last: str | None, before: int = 0, after: int = 0):
    """(row0, row1) of the last block that starts with *first* and ends at *last* (or +after rows)."""
    rows = term.screen.display
    starts = [i for i, r in enumerate(rows) if first in r]
    if not starts:
        return None
    r0 = starts[-1]
    r1 = r0 + after
    if last:
        ends = [i for i in range(r0, len(rows)) if last in rows[i]]
        r1 = ends[0] if ends else min(len(rows) - 1, r0 + after)
    return max(0, r0 - before), min(len(rows) - 1, r1)


def zoom(img: Image.Image, term, rows, amount: float, cols: int = 77) -> Image.Image:
    """Ease the frame towards the receipt/box: columns 0..cols, centred on rows (row0, row1)."""
    if not rows or amount <= 0:
        return img
    w = min(W, cols * term.cw + 60)
    h = w * TERM_H / W
    need = (rows[1] - rows[0] + 1) * term.ch + 50
    if need > h:                       # tall block: widen until it fits
        h = min(TERM_H, need)
        w = h * W / TERM_H
    x0 = max(0, term.ox - 30)
    x0 = min(x0, W - w)
    yc = term.oy + (rows[0] + rows[1] + 1) / 2 * term.ch
    y0 = min(max(0, yc - h / 2), TERM_H - h)
    a = amount * amount * (3 - 2 * amount)
    box = (x0 * a, y0 * a, W + (x0 + w - W) * a, TERM_H + (y0 + h - TERM_H) * a)
    return img.crop(tuple(int(v) for v in box)).resize((W, TERM_H), Image.LANCZOS)


def ease_in(k: int, n: int, start_frac: float) -> float:
    """0 before start_frac of the scene, then ramps to 1 over 1.2 s."""
    t0 = n * start_frac
    return max(0.0, min(1.0, (k - t0) / (1.2 * FPS)))


def main() -> None:
    head, events = load_cast(HERE / "demo.cast")
    ev = compress(events)
    marks, all_marks = {}, []
    for ct, kind, data in ev:
        if kind == "m":
            marks.setdefault(data, ct)
            all_marks.append((ct, data))
    keep_box = [ct for ct, d in all_marks if d == "approval" and marks["turn2"] < ct <= marks["turn2_done"]]
    end_ct = ev[-1][0]
    out_start = marks.get("out", marks.get("out_yes"))
    voice = json.loads((HERE / "voice" / "durations.json").read_text())
    LEAD, TAIL = 0.5, 1.0

    def slot(name, minimum):
        return round(max(minimum, voice.get(name, 0) + LEAD + TAIL), 1)

    receipt = ("LIGHTS OUT · session receipt", "prices:", 0, 0)
    box = ("plugin approval rule", "<date>", 2, 0)
    keepbox = ("plugin approval rule", "Approve only", 2, 0)
    scenes = [
        # name, start, end, seconds, caption, hold (s), zoom target, zoom starts at (fraction of scene)
        ("hook", marks["turn1_done"] - 6, marks["turn1_done"], slot("hook", 10), "Done shouldn't mean still billing.", 3, receipt, 0.25),
        ("ask", marks["turn1"], marks["approval"] + 0.3, slot("ask", 13), "Nothing billable starts without your yes, and its price", 5, box, 0.62),
        ("keep", marks["turn2"], (keep_box[-1] + 0.3) if keep_box else marks["turn2_done"], slot("keep", 12), "Keep it on purpose, until a date", 5, keepbox, 0.55),
        ("results", "card", None, slot("results", 18), "Same model, same tasks: alone, with a prompt, with Lights Out", 0, None, 0),
        ("out", out_start, marks.get("end", end_ct), slot("out", 14), "One command to turn it all off", 5, receipt, 0.6),
        ("end", "card", None, slot("end", 7), "Lights Out for Hermes Agent", 0, None, 0),
    ]
    total = sum(sc[3] for sc in scenes)
    assert total < 90, f"too long: {total}"
    silent = HERE / "lights-out-demo.silent.mp4"
    ff = subprocess.Popen(["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
                           "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                           "-crf", "18", str(silent)], stdin=subprocess.PIPE)
    prev_last = None
    starts = {}
    clock = 0.0
    XF = int(0.4 * FPS)
    for name, start, stop, secs, text, hold, target, zfrac in scenes:
        starts[name] = clock
        clock += secs
        n = int(round(secs * FPS))
        term = None
        if start != "card":
            term = Term(head["width"], head["height"])
            i = 0
            while i < len(ev) and ev[i][0] < start:
                if ev[i][1] == "o":
                    term.feed(ev[i][2])
                i += 1
            length = max(0.1, stop - start)
            speed = max(1.0, length / max(1, secs - hold))
        base = results_card() if name == "results" else end_card() if name == "end" else None
        for k in range(n):
            if term is None:
                tframe = base.copy()
                if name == "results":            # rows print one by one, like a till
                    d = ImageDraw.Draw(tframe)
                    shown = 3 + int(k / (0.55 * FPS))
                    y_cover = 150 + 46 * shown
                    if y_cover < TERM_H - 40:
                        d.rectangle((180, y_cover, W - 180, TERM_H - 40), fill=PAPER)
                sp = 1.0
            else:
                t = start + min(length, k / FPS * speed)
                while i < len(ev) and ev[i][0] <= t:
                    if ev[i][1] == "o":
                        term.feed(ev[i][2])
                    i += 1
                tframe = Image.new("RGB", (W, TERM_H), BG)
                term.draw(ImageDraw.Draw(tframe))
                if target:
                    rows = find_rows(term, target[0], target[1], target[2], 12)
                    tframe = zoom(tframe, term, rows, ease_in(k, n, zfrac))
                sp = speed if k / FPS * speed < length else 1.0
            img = Image.new("RGB", (W, H), BG)
            img.paste(tframe, (0, 0))
            caption(ImageDraw.Draw(img), text, sp)
            if prev_last is not None and k < XF:
                img = Image.blend(prev_last, img, (k + 1) / (XF + 1))
            ff.stdin.write(img.tobytes())
            last = img
        prev_last = last
    ff.stdin.close()
    ff.wait()

    # narration: each scene's clip starts LEAD s into its scene
    inputs, filters = [], []
    for j, (name, *_rest) in enumerate(scenes):
        wav = HERE / "voice" / f"{name}.wav"
        inputs += ["-i", str(wav)]
        ms = int((starts[name] + LEAD) * 1000)
        filters.append(f"[{j + 1}:a]adelay={ms}|{ms},apad[a{j}]")
    mix = "".join(f"[a{j}]" for j in range(len(scenes)))
    filt = ";".join(filters) + f";{mix}amix=inputs={len(scenes)}:normalize=0,volume=1.0,atrim=0:{total}[aout]"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), *inputs, "-filter_complex", filt,
                    "-map", "0:v", "-map", "[aout]", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
                    "-movflags", "+faststart", "-shortest", str(HERE / "lights-out-demo.mp4")], check=True)
    silent.unlink()
    print(f"demo/lights-out-demo.mp4: {total:.1f} s; scenes start at "
          + json.dumps({k: round(v, 1) for k, v in starts.items()}))


if __name__ == "__main__":
    sys.exit(main())
