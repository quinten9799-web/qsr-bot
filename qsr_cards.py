"""QSR broadcast-style cards Dale posts to Discord (Pillow only).

render_track_history(rw, race_num, date_text, logo_path) -> PNG bytes
  rw comes from qsr_history.race_week(): past winners, the field's record
  at the track, and storylines.
"""
import io
import os

from PIL import Image, ImageDraw, ImageFilter

W, H = 1080, 1350
BG = (10, 10, 12)
PANEL = (22, 22, 26)
LINE = (44, 44, 50)
WHITE = (245, 245, 245)
GREY = (150, 150, 158)
DIM = (95, 95, 104)
ORANGE = (255, 106, 0)
RED = (232, 39, 42)
GOLD = (255, 196, 0)

_HERE = os.path.dirname(os.path.abspath(__file__))
_FONTS = {"xb": "BarlowCondensed-ExtraBold.ttf", "b": "BarlowCondensed-Bold.ttf",
          "sb": "BarlowCondensed-SemiBold.ttf", "m": "BarlowCondensed-Medium.ttf"}
_CACHE = {}


def font(size, w="b"):
    k = (size, w)
    if k not in _CACHE:
        from PIL import ImageFont
        p = os.path.join(_HERE, "fonts", _FONTS[w])
        try:
            _CACHE[k] = ImageFont.truetype(p, size)
        except Exception:
            try:
                _CACHE[k] = ImageFont.load_default(size=size)
            except TypeError:
                _CACHE[k] = ImageFont.load_default()
    return _CACHE[k]


def tw(d, t, f):
    b = d.textbbox((0, 0), t, font=f)
    return b[2] - b[0]


def fit(d, t, max_w, start, w="xb", min_size=28):
    s = start
    while s > min_size and tw(d, t, font(s, w)) > max_w:
        s -= 2
    return font(s, w)


def clip(d, t, f, max_w):
    if tw(d, t, f) <= max_w:
        return t
    while t and tw(d, t + "…", f) > max_w:
        t = t[:-1]
    return t + "…"


def spaced(d, xy, text, f, fill, gap=4):
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=f, fill=fill)
        x += tw(d, ch, f) + gap
    return x


def _background():
    img = Image.new("RGB", (W, H), BG)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-420, -520, 760, 520), fill=(232, 39, 42, 70))
    g.ellipse((520, -380, 1500, 380), fill=(255, 106, 0, 45))
    glow = glow.filter(ImageFilter.GaussianBlur(160))
    img.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(img, "RGBA")
    for i, c in enumerate([(232, 39, 42, 255), (255, 106, 0, 255), (255, 150, 40, 200)]):   # racing stripes
        x = 1000 + i * 30
        d.polygon([(x, 0), (x + 18, 0), (x - 12, 50), (x - 30, 50)], fill=c)
    return img


def _panel(d, box, title):
    x0, y0, x1, y1 = box
    d.rounded_rectangle(box, radius=18, fill=PANEL + (235,), outline=LINE, width=2)
    d.rectangle((x0, y0 + 20, x0 + 6, y0 + 52), fill=ORANGE)
    spaced(d, (x0 + 26, y0 + 16), title, font(32, "b"), ORANGE, 3)
    return y0 + 66


def render_track_history(rw, race_num=None, date_text="", logo_path=None, series_tag="HHPS"):
    img = _background()
    d = ImageDraw.Draw(img, "RGBA")
    M = 60
    # logo + race tag
    top = 52
    if logo_path and os.path.exists(logo_path):
        try:
            lg = Image.open(logo_path).convert("RGBA")
            lg.thumbnail((230, 84))
            img.paste(lg, (M, top), lg)
        except Exception:
            pass
    tag = " · ".join(x for x in [f"RACE {race_num}" if race_num else "", date_text.upper(), "8PM ET"] if x)
    f = font(34, "b")
    d.text((W - M - tw(d, tag, f), top + 22), tag, font=f, fill=WHITE)
    # title
    y = 158
    spaced(d, (M, y), "TRACK HISTORY", font(36, "b"), ORANGE, 6)
    y += 44
    tname = rw["track"].upper()
    ft = fit(d, tname, W - 2 * M, 104)
    d.text((M, y), tname, font=ft, fill=WHITE)
    y += ft.size + 12
    been = len(rw["been"])
    sub = (f"{rw['races']} QSR RACE{'S' if rw['races'] != 1 else ''} HERE  ·  {been} OF THIS FIELD HAVE RACED IT"
           if rw["races"] else "QSR HAS NEVER RACED HERE  ·  CLEAN SLATE")
    d.text((M, y), sub, font=font(32, "sb"), fill=GREY)
    y += 56

    if rw["races"]:
        # past winners, newest first
        past = list(reversed(rw["past"]))[:4]
        y0 = y
        yy = _panel(d, (M, y0, W - M, y0 + 78 + 52 * len(past)), "PAST WINNERS")
        infield = {r["driver"] for r in rw["field"]}
        for w in past:
            d.text((M + 28, yy), w["year"], font=font(38, "b"), fill=ORANGE)
            name_col = WHITE if w["driver"] in infield else GREY
            nf = font(40, "b")
            d.text((M + 120, yy - 2), clip(d, w["driver"].upper(), nf, 400), font=nf, fill=name_col)
            d.text((M + 540, yy + 6), clip(d, w["series"], font(30, "m"), 250), font=font(30, "m"), fill=DIM)
            extra = f"FROM P{w['start']}" if w.get("start") else ""
            if w.get("margin") is not None and w["margin"] < 0.5:
                extra = f"BY {w['margin']:.3f}s"
            if extra:
                ef = font(30, "sb")
                d.text((W - M - 28 - tw(d, extra, ef), yy + 6), extra, font=ef, fill=GREY)
            yy += 52
        y = yy + 26

        # the field at this track
        rows = rw["been"][:5]
        y0 = y
        h = 78 + 42 + (50 * len(rows) if rows else 70)
        yy = _panel(d, (M, y0, W - M, y0 + h), "IN THIS FIELD")
        cols = [("DRIVER", M + 28), ("ST", 610), ("W", 690), ("T5", 770), ("AVG", 850), ("LED", 950)]
        hf = font(26, "sb")
        for label, x in cols:
            d.text((x, yy), label, font=hf, fill=DIM)
        yy += 42
        if not rows:
            d.text((M + 28, yy + 8), "Nobody entered has raced QSR here. Wide open.", font=font(34, "m"), fill=GREY)
        for r in rows:
            winner = r["wins"] > 0
            if winner:
                d.rounded_rectangle((M + 12, yy - 5, W - M - 12, yy + 44), radius=10, fill=(255, 106, 0, 38))
            nf = font(38, "b")
            d.text((M + 28, yy), clip(d, r["driver"].upper(), nf, 500), font=nf, fill=WHITE)
            vals = [str(r["starts"]), str(r["wins"]), str(r["top5"]), f"{r['avg']:.1f}" if r["avg"] is not None else "-",
                    str(r["led"]) if r["led"] else "-"]
            for (label, x), v in zip(cols[1:], vals):
                vf = font(38, "b")
                col = GOLD if (label == "W" and r["wins"]) else WHITE
                d.text((x, yy), v, font=vf, fill=col)
            yy += 50
        y = y0 + h + 26
    else:
        # first visit: who in the field has won the most anywhere
        if rw.get("road"):
            y0 = y
            rr = rw["road"][:4]
            h = 78 + 52 * len(rr) + 8
            yy = _panel(d, (M, y0, W - M, y0 + h), "ROAD-COURSE WINNERS IN THE FIELD")
            for r in rr:
                nf = font(40, "b")
                d.text((M + 28, yy), clip(d, r["driver"].upper(), nf, 400), font=nf, fill=WHITE)
                tt = clip(d, ", ".join(r["tracks"]), font(30, "m"), 390)
                d.text((M + 450, yy + 6), tt, font=font(30, "m"), fill=GREY)
                v = f"{r['wins']}W"
                d.text((W - M - 28 - tw(d, v, font(40, "b")), yy), v, font=font(40, "b"), fill=GOLD)
                yy += 52
            y = y0 + h + 26
        rows = sorted([r for r in rw["field"] if r["career_wins"]], key=lambda r: -r["career_wins"])[:(5 if rw.get("road") else 8)]
        y0 = y
        h = 78 + 50 * max(1, len(rows)) + 8
        yy = _panel(d, (M, y0, W - M, y0 + h), "FIELD'S ALL-TIME QSR WINNERS")
        for r in rows:
            nf = font(40, "b")
            d.text((M + 28, yy), clip(d, r["driver"].upper(), nf, 620), font=nf, fill=WHITE)
            v = f"{r['career_wins']} WIN{'S' if r['career_wins'] != 1 else ''}"
            d.text((W - M - 28 - tw(d, v, font(40, "b")), yy), v, font=font(40, "b"), fill=GOLD)
            yy += 50
        y = y0 + h + 26

    # Dale's notes
    skip = ["Last time", "QSR has never"] + (["Road-course"] if rw.get("road") else [])
    try:
        from qsr_history import pick_storylines
        notes = pick_storylines(rw, 3, skip)
    except Exception:
        notes = [x for x in rw["storylines"] if not any(x.startswith(k) for k in skip)][:3]
    if notes and y < H - 190:
        y0 = y
        nf = font(32, "m")
        lines = []
        for s in notes:
            words, cur = s.split(), ""
            for wd in words:
                t = (cur + " " + wd).strip()
                if tw(d, t, nf) > W - 2 * M - 80:
                    lines.append(("", cur))
                    cur = wd
                else:
                    cur = t
            lines.append(("", cur))
            lines.append(("gap", ""))
        lines = lines[:-1]
        room = (H - 100) - (y0 + 82)
        need = sum(40 if k == "" else 12 for k, _ in lines)
        while need > room and lines:
            # drop whole notes from the end, never half a sentence
            cut = max((i for i, (k, _) in enumerate(lines) if k == "gap"), default=-1)
            lines = lines[:cut] if cut > 0 else []
            need = sum(40 if k == "" else 12 for k, _ in lines)
        yy = _panel(d, (M, y0, W - M, y0 + 78 + need + 14), "DALE'S NOTES")
        first = True
        for k, t in lines:
            if k == "gap":
                yy += 12
                first = True
                continue
            if first:
                d.ellipse((M + 30, yy + 14, M + 42, yy + 26), fill=ORANGE)
                first = False
            d.text((M + 58, yy), t, font=nf, fill=WHITE)
            yy += 40

    # footer
    foot = "QSR RECORD BOOK  ·  ASK DALE: !trackhistory  !legacy  !headtohead"
    ff = font(26, "sb")
    d.text(((W - tw(d, foot, ff)) // 2, H - 62), foot, font=ff, fill=DIM)
    d.rectangle((0, H - 10, W, H), fill=RED)
    d.rectangle((W * 2 // 3, H - 10, W, H), fill=ORANGE)

    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()
