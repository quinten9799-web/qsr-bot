"""QSR all-time history: every series QSR has ever run, plus the live season.

Shared by Race Control (qsr_app.py / QSR Live) and Ask Dale (bot.py), so both
always tell the same story.

  qsr_history.json  frozen past series (Sim Racer Hub exports + seasons that
                    Race Control archived) and an alias map for name variants
  data.json         the live season: race_results is added on top, so every
                    race Race Control scores lands in the all-time record

Main calls:
  careers(hist, data)        -> {key: career}  (see _career for the shape)
  find(careers, text)        -> career or None (full name, last name, number-less)
  leaders(careers, stat, n)  -> sorted list for a leaderboard
  dale_context(hist, data, question, asker) -> compact text block for Dale
  archive_season(path, data, label) -> freeze the live season into history
"""
import json
import os
import re

CURRENT_ID = "hhps"
CURRENT_NAME = "QSR High Horsepower Series"
CURRENT_SHORT = "High Horsepower Series"


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"version": 1, "aliases": {}, "series": []}


def norm(name):
    s = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


def base(name):
    """Name without iRacing's duplicate-name number ('austin dowdy2' -> 'austin dowdy')."""
    return re.sub(r"\d+$", "", norm(name)).strip()


def pretty_base(name):
    return re.sub(r"\d+$", "", str(name or "")).strip()


# ── live season from data.json ─────────────────────────────────────────
def current_rows(data, label=None):
    """Per-driver totals for the season running in data.json."""
    rows = []
    for name, hist in (data.get("race_results") or {}).items():
        races = [r for r in hist or [] if isinstance(r, dict) and isinstance(r.get("finish"), (int, float))]
        if not races:
            continue
        fins = [int(r["finish"]) for r in races]
        starts = [r.get("start") for r in races if isinstance(r.get("start"), (int, float))]
        row = {"name": name, "starts": len(races), "wins": sum(1 for f in fins if f == 1),
               "top5": sum(1 for f in fins if f <= 5), "top10": sum(1 for f in fins if f <= 10),
               "avg_finish": round(sum(fins) / len(fins), 1), "best": min(fins),
               "poles": sum(1 for s in starts if s == 1) if len(starts) == len(races) else None,
               "avg_start": round(sum(starts) / len(starts), 1) if len(starts) == len(races) else None,
               "incidents": sum(int(r.get("incidents") or 0) for r in races),
               "points": sum(int(r.get("points") or 0) + int(r.get("stage_pts") or 0) + int(r.get("fastest_lap_bonus") or 0) for r in races),
               "stage_pts": sum(int(r.get("stage_pts") or 0) for r in races),
               "tracks_won": sorted({r.get("track", "") for r in races if r.get("finish") == 1 and r.get("track")})}
        rows.append(row)
    return rows


def all_series(hist, data=None):
    """Frozen series + the live season (which replaces its own archived copy
    while data.json still holds its races)."""
    out = [s for s in hist.get("series", [])]
    if data is not None:
        rows = current_rows(data)
        label = data.get("season_label") or "Season 1"
        cur_id = f"{CURRENT_ID}_{norm(label).replace(' ', '')}"
        if rows:  # the live season always wins over an archived copy of itself
            out = [s for s in out if s.get("id") != cur_id]
            out.append({"id": cur_id, "name": f"{CURRENT_NAME} {label}", "short": f"HHPS {label}",
                        "live": True, "rows": rows})
    return out


# ── careers ────────────────────────────────────────────────────────────
def _keymap(hist, series):
    """Every name variant -> one career key. Exact name first, then the
    alias map, then the same name without iRacing's number when that's unique."""
    aliases = {norm(k): norm(v) for k, v in (hist.get("aliases") or {}).items()}
    names = {norm(r["name"]) for s in series for r in s["rows"] if not r.get("ai")}
    by_base = {}
    for n in names:
        by_base.setdefault(base(n), set()).add(n)
    km = {}
    for n in names:
        k = aliases.get(n, n)
        grp = by_base.get(base(n), set())
        if k == n and len(grp) > 1:
            # 'austin dowdy' + 'austin dowdy2' are the same racer: file under the numbered one
            k = aliases.get(sorted(grp, key=lambda x: (-len(x), x))[0], sorted(grp, key=lambda x: (-len(x), x))[0])
        km[n] = k
    return km


def _wavg(parts, field):
    num = den = 0.0
    for starts, v in parts:
        if v is None:
            continue
        num += starts * v
        den += starts
    return round(num / den, 1) if den else None


def _merge_lines(lines):
    """Same series twice under two name variants (e.g. 'Austin Dowdy' and
    'Austin Dowdy2' in one season) -> one line."""
    by = {}
    for x in lines:
        k = x.get("series_id") or x["series"]
        if k not in by:
            by[k] = dict(x)
            continue
        a = by[k]
        n1, n2 = a["starts"], x["starts"]
        for f in ("avg_finish", "avg_start", "rating", "avg_pos", "arp"):
            if a.get(f) is not None and x.get(f) is not None:
                a[f] = round((a[f] * n1 + x[f] * n2) / (n1 + n2), 1)
            elif a.get(f) is None or x.get(f) is None:
                a[f] = a.get(f) if x.get(f) is None else (x[f] if a.get(f) is None else a[f])
        for f in ("starts", "wins", "top5", "incidents", "points", "stage_pts"):
            if f in a or f in x:
                a[f] = (a.get(f) or 0) + (x.get(f) or 0)
        for f in ("top10", "poles"):
            a[f] = None if a.get(f) is None or x.get(f) is None else a[f] + x[f]
        if "best" in a or "best" in x:
            a["best"] = min(v for v in (a.get("best"), x.get("best")) if v is not None)
        if "tracks_won" in a or "tracks_won" in x:
            a["tracks_won"] = sorted(set(a.get("tracks_won") or []) | set(x.get("tracks_won") or []))
    return list(by.values())


def rank_of(cs, c, stat="wins"):
    """Standard competition rank (ties share a spot)."""
    v = c["totals"][stat]
    return 1 + sum(1 for o in cs.values() if o["totals"][stat] > v)


def careers(hist, data=None):
    series = all_series(hist, data)
    km = _keymap(hist, series)
    out = {}
    for s in series:
        for r in s["rows"]:
            if r.get("ai"):
                continue
            k = km[norm(r["name"])]
            c = out.setdefault(k, {"key": k, "name": pretty_base(r["name"]), "names": set(), "lines": []})
            c["names"].add(r["name"])
            if s.get("live") or len(pretty_base(r["name"])) >= len(c["name"]):
                c["name"] = pretty_base(r["name"])
            c["lines"].append(dict(r, series=s.get("short") or s["name"], series_id=s.get("id"), live=bool(s.get("live"))))
    for c in out.values():
        c["lines"] = _merge_lines(c["lines"])
        L = c["lines"]
        c["names"] = sorted(c["names"])
        t = {"series": len(L), "starts": sum(x["starts"] for x in L), "wins": sum(x["wins"] for x in L),
             "top5": sum(x["top5"] for x in L)}
        t10 = [x.get("top10") for x in L]
        t["top10"] = sum(v for v in t10 if v is not None)
        t["top10_partial"] = any(v is None for v in t10)
        pl = [x.get("poles") for x in L]
        t["poles"] = sum(v for v in pl if v is not None)
        t["avg_finish"] = _wavg([(x["starts"], x.get("avg_finish")) for x in L], "avg_finish")
        t["avg_start"] = _wavg([(x["starts"], x.get("avg_start")) for x in L], "avg_start")
        t["rating"] = _wavg([(x["starts"], x.get("rating")) for x in L], "rating")
        t["win_pct"] = round(100 * t["wins"] / t["starts"]) if t["starts"] else 0
        t["top5_pct"] = round(100 * t["top5"] / t["starts"]) if t["starts"] else 0
        t["series_won_in"] = [x["series"] for x in L if x["wins"]]
        c["totals"] = t
    return out


def find(cs, text):
    """Career by name: exact, number-less, then unique last-name match."""
    q = norm(text)
    if not q:
        return None
    for c in cs.values():
        if q == c["key"] or q in {norm(n) for n in c["names"]} or q == base(c["key"]):
            return c
    hits = [c for c in cs.values() if q in norm(c["name"]) or any(q in norm(n) for n in c["names"])]
    if len(hits) == 1:
        return hits[0]
    last = [c for c in cs.values() if base(c["key"]).split()[-1:] == [q]]
    return last[0] if len(last) == 1 else None


def mentioned(cs, text, limit=4):
    """Careers whose full name, or unique last name, appears in free text."""
    t = " " + norm(text) + " "
    found = []
    lasts = {}
    for c in cs.values():
        lasts.setdefault(base(c["key"]).split()[-1] if base(c["key"]) else "", []).append(c)
    for c in cs.values():
        b = base(c["key"])
        if b and (" " + b + " ") in t:
            found.append(c)
    for last, grp in lasts.items():
        if len(last) >= 4 and len(grp) == 1 and (" " + last + " ") in t and grp[0] not in found:
            found.append(grp[0])
    found.sort(key=lambda c: -c["totals"]["starts"])
    return found[:limit]


STATS = {"wins": ("wins", True), "starts": ("starts", True), "poles": ("poles", True), "top5": ("top5", True),
         "top10": ("top10", True), "avg_finish": ("avg_finish", False), "rating": ("rating", True),
         "win_pct": ("win_pct", True), "series": ("series", True)}


def leaders(cs, stat="wins", n=10, min_starts=0):
    field, desc = STATS.get(stat, (stat, True))
    rows = [c for c in cs.values() if c["totals"]["starts"] >= min_starts and c["totals"].get(field) is not None]
    rows.sort(key=lambda c: ((-c["totals"][field]) if desc else c["totals"][field], -c["totals"]["wins"], -c["totals"]["starts"]))
    return rows[:n]


def career_line(c):
    t = c["totals"]
    parts = [f"{t['starts']} starts in {t['series']} series", f"{t['wins']} wins", f"{t['poles']} poles",
             f"{t['top5']} top 5s", f"{t['top10']}{'+' if t['top10_partial'] else ''} top 10s"]
    if t["avg_finish"] is not None:
        parts.append(f"avg finish {t['avg_finish']}")
    if t["rating"] is not None:
        parts.append(f"avg SRH rating {t['rating']}")
    return f"{c['name']}: " + ", ".join(parts)


def series_line(x):
    bits = [f"{x['starts']} st", f"{x['wins']} W", f"{x['top5']} T5"]
    if x.get("top10") is not None:
        bits.append(f"{x['top10']} T10")
    if x.get("poles") is not None:
        bits.append(f"{x['poles']} poles")
    bits.append(f"avg fin {x['avg_finish']}")
    if x.get("avg_start") is not None:
        bits.append(f"avg st {x['avg_start']}")
    return f"{x['series']}{' (running now)' if x.get('live') else ''}: " + ", ".join(bits)


def summary(hist, data=None):
    series = all_series(hist, data)
    races = {s.get("short") or s["name"]: sum(r["wins"] for r in s["rows"]) for s in series}
    return {"series": [s.get("short") or s["name"] for s in series], "races": races, "total_races": sum(races.values())}


def dale_context(hist, data, question="", asker=""):
    """Compact all-time block for Dale's system prompt: league totals, the
    record books, and full series-by-series lines for anyone named."""
    cs = careers(hist, data)
    if not cs:
        return ""
    sm = summary(hist, data)
    out = ["\n\nQSR ALL-TIME HISTORY (every series QSR has ever run, from Sim Racer Hub records plus the current season. "
           "Use these numbers exactly; if a stat isn't here, say you don't have it):",
           "Series: " + "; ".join(f"{k} ({v} races)" for k, v in sm["races"].items()) + f". {sm['total_races']} races all-time."]
    def board(title, stat, n=8, min_starts=0, fmt=None):
        rows = leaders(cs, stat, n, min_starts)
        fmt = fmt or (lambda c: c["totals"][STATS[stat][0]])
        out.append(f"{title}: " + ", ".join(f"{i + 1}. {c['name']} {fmt(c)}" for i, c in enumerate(rows)))
    board("ALL-TIME WINS", "wins", 10)
    board("ALL-TIME STARTS", "starts", 8)
    board("ALL-TIME POLES", "poles", 8)
    board("ALL-TIME TOP 5s", "top5", 8)
    board("BEST AVG FINISH (20+ starts)", "avg_finish", 6, 20)
    board("MOST SERIES RACED", "series", 6)
    people = mentioned(cs, f"{question} {asker}")
    me = find(cs, asker) if asker else None
    if me and me not in people:
        people.insert(0, me)
    for c in people[:4]:
        out.append("CAREER " + career_line(c))
        for x in c["lines"]:
            out.append("  - " + series_line(x))
        if c["totals"]["wins"]:
            out.append(f"  - T-{rank_of(cs, c)} all-time in wins" if sum(1 for o in cs.values() if o['totals']['wins'] == c['totals']['wins']) > 1
                       else f"  - {rank_of(cs, c)} all-time in wins")
        out.append(f"  - {rank_of(cs, c, 'starts')} all-time in starts")
    return "\n".join(out)


def talking_stats(cs, name):
    """Short all-time facts for the broadcast talking points."""
    c = find(cs, name)
    if not c:
        return None
    t = c["totals"]
    wins_rank = rank_of(cs, c)
    return {"name": c["name"], "starts": t["starts"], "wins": t["wins"], "poles": t["poles"], "top5": t["top5"],
            "series": t["series"], "avg_finish": t["avg_finish"], "wins_rank": wins_rank if t["wins"] else None}


# ── archiving a finished QSR season ────────────────────────────────────
def next_label(label):
    m = re.match(r"^(.*?)(\d+)\s*$", str(label or "Season 1"))
    return f"{m.group(1)}{int(m.group(2)) + 1}" if m else "Season 2"


def archive_season(path, data, label=None, name=None):
    """Freeze the live season from data.json into qsr_history.json so it stays
    in the all-time record after standings are reset. Safe to call twice."""
    hist = load(path)
    label = label or data.get("season_label") or "Season 1"
    sid = f"{CURRENT_ID}_{norm(label).replace(' ', '')}"
    rows = current_rows(data)
    if not rows:
        return False, "No races in data.json to archive."
    hist["series"] = [s for s in hist.get("series", []) if s.get("id") != sid]
    hist["series"].append({"id": sid, "name": name or f"{CURRENT_NAME} {label}", "short": f"HHPS {label}",
                           "source": "Race Control archive", "rows": rows,
                           "drivers": len(rows), "total_starts": sum(r["starts"] for r in rows),
                           "total_wins": sum(r["wins"] for r in rows)})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=1)
    os.replace(tmp, path)
    return True, f"Archived {label}: {len(rows)} drivers, {sum(r['wins'] for r in rows)} races."
