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

Race by race (hist["races"] from Sim Racer Hub + every scored HHPS race):
  all_races(hist, data)          -> every race with full results, oldest first
  race_records(hist, data)       -> record book (track wins, streaks, comebacks, laps led, closest finishes...)
  track_book(hist, data, track)  -> past winners and leaders at one track
  head_to_head(hist, data, a, b) -> who finished ahead when they raced each other
  driver_races(hist, data, name) -> laps led, track wins, streaks, form for one driver
  milestones(cs, name)           -> "one win from 15", "one win from tying X for 2nd"
  next_race(hist, data)          -> the next HHPS round and its track
"""
import functools
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


@functools.lru_cache(maxsize=50000)
def norm(name):
    s = re.sub(r"[^a-z0-9 ]", " ", str(name or "").lower())
    return re.sub(r"\s+", " ", s).strip()


@functools.lru_cache(maxsize=50000)
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
    out = [_fill_from_races(hist, s) for s in hist.get("series", [])]
    if data is not None:
        rows = current_rows(data)
        label = data.get("season_label") or "Season 1"
        cur_id = f"{CURRENT_ID}_{norm(label).replace(' ', '')}"
        if rows:  # the live season always wins over an archived copy of itself
            out = [s for s in out if s.get("id") != cur_id]
            out.append({"id": cur_id, "name": f"{CURRENT_NAME} {label}", "short": f"HHPS {label}",
                        "live": True, "rows": rows})
    return out



# ── race by race ───────────────────────────────────────────────────────
TRACKS = [("roval", "Charlotte Roval"), ("daytona international speedway road course", "Daytona Road Course"),
          ("indianapolis motor speedway road course", "Indianapolis Road Course"),
          ("laguna seca", "WeatherTech Raceway Laguna Seca"), ("long beach", "Long Beach Street Circuit"),
          ("willow springs", "Willow Springs International Raceway"), ("road america", "Road America"),
          ("daytona", "Daytona International Speedway"), ("talladega", "Talladega Superspeedway"),
          ("echopark", "Atlanta Motor Speedway"), ("atlanta", "Atlanta Motor Speedway"),
          ("charlotte", "Charlotte Motor Speedway"), ("auto club", "Auto Club Speedway"), ("fontana", "Auto Club Speedway"),
          ("sonoma", "Sonoma Raceway"), ("iowa", "Iowa Speedway"), ("gateway", "World Wide Technology Raceway"),
          ("world wide technology", "World Wide Technology Raceway"), ("circuit of the americas", "Circuit of the Americas"),
          ("cota", "Circuit of the Americas"), ("homestead", "Homestead-Miami Speedway"),
          ("new hampshire", "New Hampshire Motor Speedway"), ("loudon", "New Hampshire Motor Speedway"),
          ("bristol", "Bristol Motor Speedway"), ("darlington", "Darlington Raceway"), ("kentucky", "Kentucky Speedway"),
          ("las vegas", "Las Vegas Motor Speedway"), ("vegas", "Las Vegas Motor Speedway"), ("pocono", "Pocono Raceway"),
          ("texas", "Texas Motor Speedway"), ("watkins glen", "Watkins Glen International"), ("the glen", "Watkins Glen International"),
          ("chicagoland", "Chicagoland Speedway"), ("michigan", "Michigan International Speedway"),
          ("rockingham", "Rockingham Speedway"), ("the rock", "Rockingham Speedway"), ("nashville", "Nashville Superspeedway"),
          ("kansas", "Kansas Speedway"), ("indianapolis", "Indianapolis Motor Speedway"), ("indy", "Indianapolis Motor Speedway"),
          ("dover", "Dover Motor Speedway"), ("richmond", "Richmond Raceway"), ("lime rock", "Lime Rock Park"),
          ("martinsville", "Martinsville Speedway"), ("phoenix", "Phoenix Raceway"), ("north wilkesboro", "North Wilkesboro Speedway")]


def track_name(text):
    """Any spelling of a track ('[Retired] Charlotte Motor Speedway Oval - 2018',
    'EchoPark Speedway (Atlanta)', 'Homestead Miami') -> one venue name."""
    t = " " + norm(text) + " "
    for k, v in TRACKS:
        if (" " + k + " ") in t or (len(k) > 5 and k in t):
            return v
    return re.sub(r"\[[^\]]*\]", "", str(text or "")).strip()


ROAD_COURSES = {"Sonoma Raceway", "Watkins Glen International", "Circuit of the Americas", "Lime Rock Park", "Charlotte Roval",
                "Daytona Road Course", "Indianapolis Road Course", "WeatherTech Raceway Laguna Seca", "Long Beach Street Circuit",
                "Willow Springs International Raceway", "Road America"}


def track_short(track):
    """'New Hampshire Motor Speedway' -> 'New Hampshire', 'Charlotte Roval' stays."""
    t = track_name(track)
    for w in (" International Speedway", " Motor Speedway", " Superspeedway", " International Raceway", " International",
              " Speedway", " Raceway", " Street Circuit", " Park"):
        if t.endswith(w):
            return t[: -len(w)]
    return t.replace("WeatherTech Raceway ", "")


def tracks_in(text):
    t = " " + norm(text) + " "
    out = []
    for k, v in TRACKS:
        if (" " + k + " ") in t and v not in out:
            out.append(v)
    return out


def _series_names(hist, data=None):
    return {s.get("id"): (s.get("short") or s.get("name")) for s in all_series(hist, data) if s.get("id")}


def _fill_from_races(hist, s):
    """Series whose every race is in hist['races'] get exact top 10s (the SRH
    export cut that column off for part of the Chase field)."""
    cov = (hist.get("race_coverage") or {}).get(s.get("id")) or {}
    if not cov.get("complete") or not any(r.get("top10") is None for r in s.get("rows", [])):
        return s
    t10 = {}
    for race in hist.get("races") or []:
        if race.get("series") == s.get("id"):
            for x in race["results"]:
                if isinstance(x.get("fin"), int) and x["fin"] <= 10:
                    t10[norm(x["name"])] = t10.get(norm(x["name"]), 0) + 1
    s = dict(s)
    s["rows"] = [dict(r, top10=t10.get(norm(r["name"]), 0)) if r.get("top10") is None else r for r in s["rows"]]
    s.pop("note", None)
    return s


def _parse_date(text):
    import datetime as _dt
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"):
        try:
            return _dt.datetime.strptime(str(text).strip(), fmt).date().isoformat()
        except Exception:
            pass
    return ""


def live_races(data):
    """Scored HHPS races from data.json in the same shape as the SRH races.
    (No start spot or laps led: Race Control doesn't store them per race.)"""
    if not data:
        return []
    label = data.get("season_label") or "Season 1"
    cur_id = f"{CURRENT_ID}_{norm(label).replace(' ', '')}"
    tracks = {}
    for name, rows in (data.get("race_results") or {}).items():
        for r in rows or []:
            if isinstance(r, dict) and r.get("race") and r.get("track"):
                tracks[int(r["race"])] = r["track"]
    out = []
    for key, rh in (data.get("race_history") or {}).items():
        try:
            n = int(rh.get("race_number") or str(key).split("_")[-1])
        except Exception:
            continue
        res = []
        for x in rh.get("results") or []:
            if isinstance(x.get("pos"), (int, float)) and x.get("name"):
                res.append({"fin": int(x["pos"]), "st": None, "name": x["name"], "laps": None, "led": None,
                            "inc": x.get("incidents"), "status": "", "int": "", "fl": None, "pts": x.get("total_pts")})
        if not res:
            continue
        out.append({"id": f"{cur_id}_r{n}", "series": cur_id, "round": n, "date": _parse_date(rh.get("date")) or "",
                    "track": track_name(tracks.get(n, "")), "laps": None, "results": sorted(res, key=lambda x: x["fin"]),
                    "live": True})
    return out


def all_races(hist, data=None):
    """AI drivers (Route 66) keep their finishing spots but never enter the records."""
    races = [dict(r, results=[x for x in r["results"] if not x.get("ai")]) if any(x.get("ai") for x in r["results"]) else r
             for r in hist.get("races") or []]
    races += live_races(data)
    races.sort(key=lambda r: (r.get("date") or "9999", str(r.get("series")), r.get("round") or 0))
    return races


class _Names:
    """Race-sheet name -> career, built once per call (fast enough for a 2s refresh)."""
    def __init__(self, cs):
        self.cs, self.by = cs, {}
        for c in cs.values():
            for n in list(c["names"]) + [c["key"]]:
                self.by.setdefault(norm(n), c["key"])
        self.base = {}
        for c in cs.values():
            self.base.setdefault(base(c["key"]), set()).add(c["key"])
        self.raw = {}

    def key(self, name):
        n = norm(name)
        if n in self.by:
            return self.by[n]
        grp = self.base.get(base(n)) or set()
        return next(iter(grp)) if len(grp) == 1 else n

    def seen(self, raw):
        """Remember race-sheet spelling ('Jonny DiPasquale') over export casing."""
        self.raw.setdefault(self.key(raw), pretty_base(raw))

    def name(self, k):
        c = self.cs.get(k)
        r = self.raw.get(k)
        if c and r and norm(r) == norm(c["name"]):
            return r
        return c["name"] if c else (r or pretty_base(k).title())


def _margin(race):
    """Winning margin in seconds, when P2 was on the lead lap."""
    R = sorted([x for x in race["results"] if isinstance(x.get("fin"), int)], key=lambda x: x["fin"])
    if len(R) < 2 or R[0]["fin"] != 1 or R[1]["fin"] != 2:
        return None
    def f(v):
        try:
            return abs(float(str(v).strip()))
        except Exception:
            return None
    a, b = f(R[0].get("int")) or 0.0, f(R[1].get("int"))
    if b is None or "L" in str(R[1].get("int")):
        return None
    m = round(b - a, 3)
    return m if m >= 0 else None


def _ctx(hist, data, cs=None):
    cs = cs if cs is not None else careers(hist, data)
    nm, races = _Names(cs), all_races(hist, data)
    for r in races:
        for x in r["results"]:
            nm.seen(x["name"])
    return cs, nm, races, _series_names(hist, data)


def race_label(r, sn=None):
    sn = sn or {}
    yr = (r.get("date") or "")[:4]
    return f"{r.get('track')} {yr} ({sn.get(r.get('series'), r.get('series'))})"


def _streaks(races, nm, test):
    """Longest run of consecutive starts (within one series) where test(fin) holds."""
    best = {}
    cur = {}
    for r in races:
        sid = r.get("series")
        for x in r["results"]:
            if not isinstance(x.get("fin"), int):
                continue
            k = (nm.key(x["name"]), sid)
            if test(x["fin"]):
                run = cur.get(k, (0, None))[0] + 1
                start = cur.get(k, (0, r))[1] or r
                cur[k] = (run, start)
                if run > best.get(k[0], (0,))[0]:
                    best[k[0]] = (run, sid, start, r)
            else:
                cur[k] = (0, None)
    return best, cur


def race_records(hist, data=None, cs=None):
    """The record book, built from every race with full results."""
    cs, nm, races, sn = _ctx(hist, data, cs)
    if not races:
        return []
    recs = []
    wins_at = {}
    for r in races:
        for x in r["results"]:
            if x.get("fin") == 1:
                k = (nm.key(x["name"]), r["track"])
                wins_at[k] = wins_at.get(k, 0) + 1
    if wins_at:
        top = max(wins_at.values())
        who = [f"{nm.name(k)} at {t}" for (k, t), v in wins_at.items() if v == top]
        recs.append({"id": "track_wins", "record": "Most wins at one track", "value": f"{top} wins", "detail": "; ".join(who[:4])})
    for label, test, rid in (("Longest winning streak", lambda f: f == 1, "win_streak"),
                             ("Longest top-5 streak", lambda f: f <= 5, "top5_streak"),
                             ("Longest top-10 streak", lambda f: f <= 10, "top10_streak")):
        best, _ = _streaks(races, nm, test)
        if best:
            top = max(v[0] for v in best.values())
            who = [f"{nm.name(k)} ({sn.get(v[1], v[1])}, {v[2].get('track')} {v[2].get('date', '')[:4]} to {v[3].get('track')} {v[3].get('date', '')[:4]})"
                   for k, v in best.items() if v[0] == top]
            recs.append({"id": rid, "record": label, "value": f"{top} straight starts", "detail": "; ".join(who[:3])})
    full = [r for r in races if not r.get("live")]
    comebacks = []
    for r in full:
        w = next((x for x in r["results"] if x.get("fin") == 1), None)
        if w and isinstance(w.get("st"), int):
            comebacks.append((w["st"], w, r))
    if comebacks:
        comebacks.sort(key=lambda t: -t[0])
        st, w, r = comebacks[0]
        more = "; ".join(f"{nm.name(nm.key(x['name']))} from P{s}, {race_label(rr, sn)}" for s, x, rr in comebacks[1:3])
        recs.append({"id": "comeback", "record": "Deepest starting spot to win", "value": f"P{st}",
                     "detail": f"{nm.name(nm.key(w['name']))}, {race_label(r, sn)}" + (f". Next: {more}" if more else "")})
    led = [(x.get("led") or 0, x, r) for r in full for x in r["results"]]
    if led:
        led.sort(key=lambda t: -t[0])
        n, x, r = led[0]
        recs.append({"id": "laps_led_race", "record": "Most laps led in one race", "value": f"{n} of {r.get('laps')}",
                     "detail": f"{nm.name(nm.key(x['name']))}, {race_label(r, sn)}"})
    dom = [((x.get("led") or 0) / r["laps"], x, r) for r in full if r.get("laps") for x in r["results"] if x.get("fin") == 1]
    if dom:
        dom.sort(key=lambda t: -t[0])
        p, x, r = dom[0]
        recs.append({"id": "dominant", "record": "Most dominant win", "value": f"led {round(100 * p)}% of laps",
                     "detail": f"{nm.name(nm.key(x['name']))}, {race_label(r, sn)}"})
    tot = {}
    for n, x, r in led:
        tot[nm.key(x["name"])] = tot.get(nm.key(x["name"]), 0) + n
    if tot:
        ranked = sorted(tot.items(), key=lambda kv: -kv[1])[:3]
        recs.append({"id": "laps_led_total", "record": "Most laps led (races with lap data)", "value": f"{ranked[0][1]} laps",
                     "detail": ", ".join(f"{nm.name(k)} {v}" for k, v in ranked)})
    close = [(m, r) for r in full for m in [_margin(r)] if m is not None]
    if close:
        close.sort(key=lambda t: t[0])
        recs.append({"id": "closest", "record": "Closest finishes", "value": f"{close[0][0]:.3f}s",
                     "detail": "; ".join(f"{m:.3f}s {nm.name(nm.key(r['results'][0]['name']))} over "
                                         f"{nm.name(nm.key(sorted(r['results'], key=lambda x: x['fin'])[1]['name']))}, {race_label(r, sn)}"
                                         for m, r in close[:3])})
    p2w = {}
    for r in full:
        for x in r["results"]:
            if x.get("fin") == 1 and x.get("st") == 1:
                p2w[nm.key(x["name"])] = p2w.get(nm.key(x["name"]), 0) + 1
    if p2w:
        ranked = sorted(p2w.items(), key=lambda kv: -kv[1])[:3]
        recs.append({"id": "pole_to_win", "record": "Most pole-to-win victories", "value": f"{ranked[0][1]}",
                     "detail": ", ".join(f"{nm.name(k)} {v}" for k, v in ranked)})
    seas = {}
    for r in races:
        s = r.get("srh_season") or r.get("series")
        w = next((x for x in r["results"] if x.get("fin") == 1), None)
        if w:
            seas.setdefault((s, r.get("series")), {}).setdefault(nm.key(w["name"]), []).append(r)
    best = None
    for (s, sid), m in seas.items():
        for k, rs in m.items():
            if best is None or len(rs) > best[0]:
                best = (len(rs), k, sid, rs)
    if best:
        recs.append({"id": "season_wins", "record": "Most wins in one season", "value": f"{best[0]} wins",
                     "detail": f"{nm.name(best[1])}, {sn.get(best[2], best[2])} {best[3][0].get('date', '')[:4]}"})
    return recs


def track_book(hist, data, track, cs=None, n=5):
    """Everything QSR has done at one track."""
    cs, nm, races, sn = _ctx(hist, data, cs)
    t = track_name(track)
    rs = [r for r in races if r.get("track") == t]
    if not rs:
        return None
    winners = []
    stats = {}
    for r in rs:
        for x in r["results"]:
            if not isinstance(x.get("fin"), int):
                continue
            k = nm.key(x["name"])
            s = stats.setdefault(k, {"starts": 0, "wins": 0, "top5": 0, "fins": [], "led": 0})
            s["starts"] += 1
            s["wins"] += x["fin"] == 1
            s["top5"] += x["fin"] <= 5
            s["fins"].append(x["fin"])
            s["led"] += x.get("led") or 0
            if x["fin"] == 1:
                winners.append({"date": r.get("date"), "series": sn.get(r.get("series"), r.get("series")), "driver": nm.name(k),
                                "start": x.get("st"), "margin": _margin(r)})
    lead = sorted(stats.items(), key=lambda kv: (-kv[1]["wins"], -kv[1]["top5"], sum(kv[1]["fins"]) / len(kv[1]["fins"])))
    avg = sorted([(k, v) for k, v in stats.items() if v["starts"] >= 2], key=lambda kv: sum(kv[1]["fins"]) / len(kv[1]["fins"]))
    return {"track": t, "races": len(rs), "winners": winners,
            "leaders": [{"driver": nm.name(k), "wins": v["wins"], "top5": v["top5"], "starts": v["starts"],
                         "avg_finish": round(sum(v["fins"]) / len(v["fins"]), 1), "led": v["led"]} for k, v in lead[:n]],
            "best_avg": [{"driver": nm.name(k), "avg_finish": round(sum(v["fins"]) / len(v["fins"]), 1), "starts": v["starts"]}
                         for k, v in avg[:n]],
            "most_led": [{"driver": nm.name(k), "led": v["led"]} for k, v in sorted(stats.items(), key=lambda kv: -kv[1]["led"])[:3] if v["led"]]}


def track_line(tb):
    if not tb:
        return ""
    w = "; ".join(f"{x['date'][:4]} {x['series']}: {x['driver']}" + (f" from P{x['start']}" if x.get("start") else "") for x in tb["winners"])
    ld = ", ".join(f"{x['driver']} {x['wins']}W/{x['top5']}T5 in {x['starts']} (avg {x['avg_finish']})" for x in tb["leaders"])
    out = f"{tb['track']}: {tb['races']} QSR races. Winners: {w}. Best here: {ld}."
    if tb["best_avg"]:
        out += " Best avg finish (2+ starts): " + ", ".join(f"{x['driver']} {x['avg_finish']}" for x in tb["best_avg"][:3]) + "."
    if tb["most_led"]:
        out += " Most laps led: " + ", ".join(f"{x['driver']} {x['led']}" for x in tb["most_led"]) + "."
    return out


def head_to_head(hist, data, a, b, cs=None):
    cs, nm, races, sn = _ctx(hist, data, cs)
    ca, cb = find(cs, a), find(cs, b)
    if not ca or not cb or ca is cb:
        return None
    ka, kb = ca["key"], cb["key"]
    out = {"a": ca["name"], "b": cb["name"], "races": 0, "a_ahead": 0, "b_ahead": 0, "a_wins": 0, "b_wins": 0,
           "a_fins": [], "b_fins": [], "last": None}
    for r in races:
        fa = fb = None
        for x in r["results"]:
            k = nm.key(x["name"])
            if k == ka:
                fa = x.get("fin")
            elif k == kb:
                fb = x.get("fin")
        if isinstance(fa, int) and isinstance(fb, int):
            out["races"] += 1
            out["a_ahead" if fa < fb else "b_ahead"] += 1
            out["a_wins"] += fa == 1
            out["b_wins"] += fb == 1
            out["a_fins"].append(fa)
            out["b_fins"].append(fb)
            out["last"] = f"{race_label(r, sn)}: {ca['name']} P{fa}, {cb['name']} P{fb}"
    if not out["races"]:
        return out
    out["a_avg"] = round(sum(out["a_fins"]) / len(out["a_fins"]), 1)
    out["b_avg"] = round(sum(out["b_fins"]) / len(out["b_fins"]), 1)
    return out


def h2h_line(h):
    if not h or not h["races"]:
        return f"HEAD TO HEAD {h['a']} vs {h['b']}: never in the same race with results on file." if h else ""
    return (f"HEAD TO HEAD {h['a']} vs {h['b']}: {h['races']} races together, {h['a']} finished ahead {h['a_ahead']}, "
            f"{h['b']} {h['b_ahead']}. Wins in those races {h['a_wins']}-{h['b_wins']}. Avg finish {h['a_avg']} vs {h['b_avg']}. "
            f"Last meeting {h['last']}.")


def driver_races(hist, data, name, cs=None):
    """Race-level profile: laps led, track wins, best streaks, recent form."""
    cs, nm, races, sn = _ctx(hist, data, cs)
    c = find(cs, name)
    if not c:
        return None
    k = c["key"]
    mine = []
    for r in races:
        for x in r["results"]:
            if nm.key(x["name"]) == k and isinstance(x.get("fin"), int):
                mine.append((r, x))
    if not mine:
        return {"name": c["name"], "races": 0}
    tw = {}
    for r, x in mine:
        if x["fin"] == 1:
            tw[r["track"]] = tw.get(r["track"], 0) + 1
    best_track = {}
    for r, x in mine:
        b = best_track.get(r["track"])
        if b is None or x["fin"] < b:
            best_track[r["track"]] = x["fin"]
    wins = [(r, x) for r, x in mine if x["fin"] == 1]
    out = {"name": c["name"], "races": len(mine), "laps_led": sum(x.get("led") or 0 for r, x in mine),
           "races_led": sum(1 for r, x in mine if (x.get("led") or 0) > 0),
           "track_wins": dict(sorted(tw.items(), key=lambda kv: -kv[1])),
           "best_by_track": best_track,
           "first_win": race_label(wins[0][0], sn) if wins else None,
           "last_win": race_label(wins[-1][0], sn) if wins else None,
           "best_comeback": None, "form": [f"P{x['fin']} {r['track'].split(' ')[0]}" for r, x in mine[-5:]]}
    cb = [(x["st"], r) for r, x in wins if isinstance(x.get("st"), int)]
    if cb:
        st, r = max(cb, key=lambda t: t[0])
        out["best_comeback"] = f"won from P{st}, {race_label(r, sn)}"
    for label, test in (("win", lambda f: f == 1), ("top5", lambda f: f <= 5), ("top10", lambda f: f <= 10)):
        best, cur = _streaks(races, nm, test)
        b = best.get(k)
        out[f"best_{label}_streak"] = b[0] if b else 0
        live = [v[0] for (kk, sid), v in cur.items() if kk == k and sid == mine[-1][0].get("series")]
        out[f"current_{label}_streak"] = live[0] if live else 0
    return out


def driver_race_line(d):
    if not d or not d.get("races"):
        return ""
    bits = [f"{d['races']} races with full results"]
    if d["laps_led"]:
        bits.append(f"{d['laps_led']} laps led in {d['races_led']} races (SRH races only)")
    if d["track_wins"]:
        bits.append("wins by track: " + ", ".join(f"{t} {n}" for t, n in d["track_wins"].items()))
    if d["first_win"]:
        bits.append(f"first win {d['first_win']}, latest {d['last_win']}")
    if d["best_comeback"]:
        bits.append(f"best comeback {d['best_comeback']}")
    pl = lambda n, w: f"{n} {w}" + ("" if n == 1 else ("s" if not w.endswith(("5", "10")) else "s"))
    bits.append(f"best streaks in a row: {pl(d['best_win_streak'], 'win')}, {pl(d['best_top5_streak'], 'top 5')}, "
                f"{pl(d['best_top10_streak'], 'top 10')}")
    if d["current_top10_streak"] >= 2:
        bits.append(f"on a {d['current_top10_streak']}-race top-10 streak right now")
    bits.append("last 5: " + ", ".join(d["form"]))
    return f"RACE LOG {d['name']}: " + "; ".join(bits)


MILESTONES = [5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 125, 150, 200, 250, 300]


def milestones(cs, name, within=1):
    """Round numbers and all-time spots a driver is close to."""
    c = find(cs, name)
    if not c:
        return []
    t = c["totals"]
    out = []
    for stat, word, w in (("wins", "win", within), ("starts", "start", within), ("top5", "top 5", within),
                          ("poles", "pole", within), ("top10", "top 10", within)):
        v = t.get(stat) or 0
        nxt = next((m for m in MILESTONES if m > v), None)
        if nxt and 0 < nxt - v <= w:
            out.append(f"{nxt - v} {word}{'s' if nxt - v > 1 and not word.endswith('5') and not word.endswith('10') else ''} from {nxt} career {word}{'s' if not word.endswith('5') and not word.endswith('10') else 's'}")
    v = t["wins"]
    if v:
        ahead = sorted({o["totals"]["wins"] for o in cs.values() if o["totals"]["wins"] > v})
        if ahead:
            tgt = ahead[0]
            if tgt - v <= 2:
                names = [o["name"] for o in cs.values() if o["totals"]["wins"] == tgt]
                spot = 1 + sum(1 for o in cs.values() if o["totals"]["wins"] > tgt)
                out.append(f"{tgt - v} win{'s' if tgt - v > 1 else ''} from tying {', '.join(names[:2])} for {_ord(spot)} all-time ({tgt})")
    return out


def _ord(n):
    return f"{n}{'th' if 10 <= n % 100 <= 20 else {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th')}"


def next_race(hist, data):
    """Next HHPS round from hist['current_schedule'] and data['race_number']."""
    sched = hist.get("current_schedule") or []
    try:
        n = int((data or {}).get("race_number") or 0)
    except Exception:
        n = 0
    done = {int(k.split("_")[-1]) for k in ((data or {}).get("race_history") or {}) if str(k).split("_")[-1].isdigit()}
    if done and n in done:
        n += 1
    for x in sched:
        if x.get("round") == n:
            return dict(x, track=track_name(x.get("track")))
    return None



# ── race week: track history for the drivers actually in the field ────
def race_week(hist, data, track, field=None, cs=None):
    """Everything Dale needs for race week at one track.
    field: names of drivers entered this week (registration), or None for everyone.
    Returns track, past winners, per-driver history for the field, and ranked storylines."""
    cs, nm, races, sn = _ctx(hist, data, cs)
    t = track_name(track)
    rs = [r for r in races if r.get("track") == t]
    past = []
    per = {}
    for r in rs:
        R = sorted([x for x in r["results"] if isinstance(x.get("fin"), int)], key=lambda x: x["fin"])
        for x in R:
            k = nm.key(x["name"])
            p = per.setdefault(k, {"key": k, "starts": 0, "wins": 0, "top5": 0, "top10": 0, "fins": [], "led": 0,
                                   "best": None, "win_years": [], "comeback": None})
            p["starts"] += 1
            p["wins"] += x["fin"] == 1
            p["top5"] += x["fin"] <= 5
            p["top10"] += x["fin"] <= 10
            p["fins"].append(x["fin"])
            p["led"] += x.get("led") or 0
            p["best"] = x["fin"] if p["best"] is None else min(p["best"], x["fin"])
            if x["fin"] == 1:
                p["win_years"].append((r.get("date") or "")[:4])
                if isinstance(x.get("st"), int) and (p["comeback"] is None or x["st"] > p["comeback"]):
                    p["comeback"] = x["st"]
        if R:
            past.append({"date": r.get("date"), "year": (r.get("date") or "")[:4], "series": sn.get(r.get("series"), r.get("series")),
                         "driver": nm.name(nm.key(R[0]["name"])), "start": R[0].get("st"), "margin": _margin(r),
                         "led": R[0].get("led"), "laps": r.get("laps"), "live": bool(r.get("live"))})
    # who's in the field
    if field is None:
        keys = list(per)
    else:
        keys = []
        for n in field:
            c = find(cs, n)
            k = c["key"] if c else nm.key(n)
            if k not in keys:
                keys.append(k)
    rows = []
    for k in keys:
        c = cs.get(k)
        p = per.get(k)
        row = {"driver": nm.name(k) if (c or p) else pretty_base(k).title(), "key": k,
               "starts": p["starts"] if p else 0, "wins": p["wins"] if p else 0, "top5": p["top5"] if p else 0,
               "top10": p["top10"] if p else 0, "best": p["best"] if p else None,
               "avg": round(sum(p["fins"]) / len(p["fins"]), 1) if p else None, "led": p["led"] if p else 0,
               "win_years": p["win_years"] if p else [], "comeback": p["comeback"] if p else None,
               "career_wins": c["totals"]["wins"] if c else 0, "career_starts": c["totals"]["starts"] if c else 0,
               "titles": c["totals"].get("titles", 0) if c else 0}
        rows.append(row)
    rows.sort(key=lambda r: (-r["wins"], -r["top5"], r["avg"] if r["avg"] is not None else 99, -r["career_wins"]))
    been = [r for r in rows if r["starts"]]
    fresh = [r for r in rows if not r["starts"]]
    story = []
    winners = [r for r in been if r["wins"]]
    if not rs:
        story.append(f"QSR has never raced at {t}. Clean slate for the whole field.")
    else:
        last = past[-1]
        story.append(f"Last time QSR raced here: {last['driver']} won ({last['year']} {last['series']})"
                     + (f" from P{last['start']}" if last.get("start") else "") + ".")
        if winners:
            if len(winners) == 1:
                w = winners[0]
                story.append(f"{w['driver']} is the only past {track_short(t)} winner in the field"
                             + (f" ({w['wins']} wins here)" if w["wins"] > 1 else "") + ".")
            else:
                story.append(f"{len(winners)} past winners here in the field: "
                             + ", ".join(f"{w['driver']}" + (f" ({w['wins']})" if w["wins"] > 1 else "") for w in winners[:4]) + ".")
        elif been:
            story.append(f"Nobody in this field has won here. Somebody's getting their first {track_short(t)} trophy.")
        perfect = [r for r in been if r["starts"] >= 2 and max(per[r["key"]]["fins"]) <= 5]
        for r in perfect[:2]:
            story.append(f"{r['driver']} has never finished outside the top 5 here ({r['starts']} starts, avg {r['avg']}).")
        led = sorted([r for r in been if r["led"]], key=lambda r: -r["led"])
        if led and led[0]["led"] >= 20:
            story.append(f"{led[0]['driver']} has led {led[0]['led']} laps here, the most in the field.")
        cb = [r for r in winners if r["comeback"] and r["comeback"] >= 10]
        if cb:
            r = max(cb, key=lambda r: r["comeback"])
            story.append(f"{r['driver']} once won here from P{r['comeback']}.")
        close = [w for w in past if w.get("margin") is not None and w["margin"] < 0.1]
        if close:
            w = min(close, key=lambda w: w["margin"])
            story.append(f"Closest finish here: {w['driver']} by {w['margin']:.3f}s ({w['year']}).")
    if fresh and rs:
        vets = [r for r in fresh if r["career_starts"] >= 10]
        story.append(f"{len(fresh)} driver{'s' if len(fresh) != 1 else ''} in the field "
                     f"{'have' if len(fresh) != 1 else 'has'} never raced QSR here"
                     + (f", including {', '.join(r['driver'] for r in vets[:2])}" if vets else "") + ".")
    ms_lines = []
    for r in rows:
        for m in (milestones(cs, r["driver"]) if r["career_starts"] else []):
            spot = re.search(r"for (\d+)(?:st|nd|rd|th) all-time", m)
            rnd = re.search(r"from (\d+) career wins", m)
            if spot and int(spot.group(1)) <= 10:
                ms_lines.append((int(spot.group(1)), f"{r['driver']}: {m}."))
            elif rnd:
                ms_lines.append((0, f"{r['driver']}: {m}."))
    ms_lines.sort(key=lambda t: t[0])
    story.extend(m for _, m in ms_lines[:2])
    milestone_lines = [m for _, m in ms_lines]
    road = None
    if t in ROAD_COURSES:
        fk = {r["key"] for r in rows}
        rw_ = {}
        for r in races:
            if r.get("track") in ROAD_COURSES and r.get("track") != t:
                w = next((x for x in r["results"] if x.get("fin") == 1), None)
                if w and nm.key(w["name"]) in fk:
                    rw_.setdefault(nm.key(w["name"]), []).append(track_short(r["track"]))
        road = sorted(({"driver": nm.name(k), "wins": len(v), "tracks": sorted(set(v))} for k, v in rw_.items()),
                      key=lambda x: -x["wins"])
        if road:
            story.insert(1 if rs else 1, "Road-course winners in the field: " + ", ".join(
                f"{x['driver']} ({', '.join(x['tracks'])})" for x in road[:4]) + ".")
        elif not rs:
            story.insert(1, "Nobody in this field has won a QSR road course yet. First one's up for grabs.")
    return {"track": t, "races": len(rs), "past": past, "field": rows, "been": been, "fresh": fresh,
            "road": road, "storylines": story, "milestones": milestone_lines}


def pick_storylines(rw, n=3, skip=()):
    """Up to n storylines, spread across different drivers."""
    names = [r["driver"] for r in rw.get("field", [])] + [w["driver"] for w in rw.get("past", [])]
    used, out = set(), []
    for s in rw.get("storylines", []):
        if any(s.startswith(x) for x in skip):
            continue
        who = {x for x in names if x and x in s}
        if who and who <= used:
            continue
        if not s.startswith("Last time"):   # the recap line shouldn't block the better one about the same driver
            used |= who
        out.append(s)
        if len(out) == n:
            break
    return out


def race_week_text(rw, max_lines=5):
    """Compact text block for announcements and Dale's prompts."""
    if not rw:
        return ""
    return "\n".join("• " + s for s in rw["storylines"][:max_lines])



# ── history made, live alerts, throwbacks ──────────────────────────────
def _without_race(data, race_num):
    """data.json as it stood before race_num was scored."""
    d = dict(data or {})
    d["race_history"] = {k: v for k, v in (d.get("race_history") or {}).items()
                         if str(k).split("_")[-1] != str(race_num)}
    d["race_results"] = {n: [r for r in (rows or []) if not (isinstance(r, dict) and r.get("race") == race_num)]
                         for n, rows in (d.get("race_results") or {}).items()}
    return d


def _nth(n):
    return _ord(n)


def history_made(hist, data, race_num):
    """What this HHPS race changed in the QSR record books, as short lines."""
    before, after = careers(hist, _without_race(data, race_num)), careers(hist, data)
    races = all_races(hist, data)
    label = data.get("season_label") or "Season 1"
    rid = f"{CURRENT_ID}_{norm(label).replace(' ', '')}_r{race_num}"
    race = next((r for r in races if r.get("id") == rid), None)
    if not race:
        return []
    nm = _Names(after)
    for r in races:
        for x in r["results"]:
            nm.seen(x["name"])
    out = []
    R = sorted(race["results"], key=lambda x: x["fin"])
    win = R[0]
    wk = nm.key(win["name"])
    ca, cb = after.get(wk), before.get(wk)
    t = race.get("track")
    if ca:
        n, st = ca["totals"]["wins"], ca["totals"]["starts"]
        if n == 1:
            out.append(f"{ca['name']} gets a first career QSR win, in start #{st}.")
        else:
            ra = rank_of(after, ca)
            rb = rank_of(before, cb) if cb else None
            tied = [o["name"] for o in after.values() if o is not ca and o["totals"]["wins"] == n]
            if rb and ra < rb:
                if tied:
                    out.append(f"{ca['name']} (win #{n}) moves into a tie for {_nth(ra)} all-time with {', '.join(tied[:2])}.")
                else:
                    passed = [o["name"] for o in before.values() if o["key"] != wk and o["totals"]["wins"] == n - 1]
                    out.append(f"{ca['name']} (win #{n}) moves to {_nth(ra)} all-time in wins"
                               + (f", passing {', '.join(passed[:2])}." if passed else "."))
            elif n in MILESTONES:
                out.append(f"{ca['name']} hits {n} career QSR wins.")
        tw = sum(1 for r in races if r.get("track") == t for x in r["results"] if x.get("fin") == 1 and nm.key(x["name"]) == wk)
        here = [r for r in races if r.get("track") == t]
        if tw == 1 and len(here) > 1:
            out.append(f"First QSR win at {track_short(t)} for {ca['name']} ({len(here)} QSR races there now).")
        elif tw >= 2:
            best = max((sum(1 for r in here for x in r["results"] if x.get("fin") == 1 and nm.key(x["name"]) == k)
                        for k in {nm.key(x["name"]) for r in here for x in r["results"] if x.get("fin") == 1}), default=0)
            out.append(f"{ca['name']} now has {tw} wins at {track_short(t)}" + (", the most in QSR history." if tw >= best else "."))
    # round-number milestones for anyone in the race
    for x in R:
        k = nm.key(x["name"])
        a, b = after.get(k), before.get(k)
        if not a or not b:
            continue
        for stat, word in (("starts", "starts"), ("top5", "top 5s"), ("top10", "top 10s")):
            va, vb = a["totals"].get(stat) or 0, b["totals"].get(stat) or 0
            hit = [m for m in MILESTONES if vb < m <= va and m >= (25 if stat == "starts" else 10)]
            if hit:
                out.append(f"{a['name']} reaches {hit[-1]} career QSR {word}.")
    # streaks that tie or set the all-time record
    recs = {r["id"]: r for r in race_records(hist, data, after)}
    for label2, test, rid2 in (("winning streak", lambda f: f == 1, "win_streak"), ("top-5 streak", lambda f: f <= 5, "top5_streak"),
                               ("top-10 streak", lambda f: f <= 10, "top10_streak")):
        _, cur = _streaks(races, nm, test)
        rec = recs.get(rid2)
        top = int(rec["value"].split()[0]) if rec else 0
        for (k, sid), v in cur.items():
            if sid == race.get("series") and v[0] >= max(top, 3) and any(nm.key(x["name"]) == k for x in R):
                out.append(f"{nm.name(k)} is on a {v[0]}-race {label2}, {'tying' if v[0] == top else 'setting'} the QSR record.")
    seen, uniq = set(), []
    for o in out:
        if o not in seen:
            seen.add(o)
            uniq.append(o)
    return uniq


def live_alerts(cs, leader, track=None, hist=None, data=None):
    """What it would mean if the current leader wins. For the broadcast ticker/talking points."""
    c = find(cs, leader)
    if not c:
        return [f"{pretty_base(leader)} is chasing a first career QSR win."]
    t = c["totals"]
    out = []
    n = t["wins"] + 1
    if n == 1:
        out.append(f"{c['name']} is leading, chasing a first career QSR win in start #{t['starts'] + 1}.")
    else:
        tied = sorted([o for o in cs.values() if o is not c and o["totals"]["wins"] == n], key=lambda o: o["name"])
        spot = 1 + sum(1 for o in cs.values() if o is not c and o["totals"]["wins"] > n)
        if tied:
            out.append(f"A win ties {c['name']} with {', '.join(o['name'] for o in tied[:2])} for {_ord(spot)} all-time ({n} wins).")
        elif n in MILESTONES:
            out.append(f"A win would be number {n} for {c['name']}.")
        else:
            above = [o for o in cs.values() if o["totals"]["wins"] == t["wins"] and o is not c]
            if above:
                out.append(f"A win breaks {c['name']}'s tie with {above[0]['name']}, moving to sole {_ord(spot)} all-time ({n}).")
    if track and hist is not None:
        tb = track_book(hist, data, track, cs, n=99)
        me = next((x for x in (tb or {}).get("leaders", []) if x["driver"] == c["name"]), None)
        if not me or not me["wins"]:
            out.append(f"{c['name']} has never won at {track_short(track)} in QSR.")
        else:
            out.append(f"{c['name']} already has {me['wins']} QSR win{'s' if me['wins'] != 1 else ''} at {track_short(track)}.")
    return out


def throwback(hist, data, today, track=None, used=(), cs=None):
    """A past race for 'This Week in QSR History'. Prefers this week's track,
    then the race closest to this calendar week in an earlier year."""
    import datetime as _dt
    cs, nm, races, sn = _ctx(hist, data, cs)
    pool = [r for r in races if not r.get("live") and r["id"] not in set(used) and r.get("date")]
    if not pool:
        pool = [r for r in races if not r.get("live") and r.get("date")]
    if not pool:
        return None
    t = track_name(track) if track else None
    doy = today.timetuple().tm_yday

    def dist(r):
        d = _dt.date.fromisoformat(r["date"]).timetuple().tm_yday
        return min(abs(d - doy), 365 - abs(d - doy))
    at = [r for r in pool if t and r["track"] == t]
    pick = sorted(at, key=lambda r: r["date"])[0] if at else min(pool, key=dist)
    R = sorted([x for x in pick["results"] if isinstance(x.get("fin"), int)], key=lambda x: x["fin"])
    w = R[0]
    years = today.year - int(pick["date"][:4])
    field = {norm(n) for n in ((data or {}).get("standings") or {})}
    top5 = [{"fin": x["fin"], "driver": nm.name(nm.key(x["name"])), "start": x.get("st"), "led": x.get("led") or 0,
             "now": norm(x["name"]) in field or any(norm(n) == norm(x["name"]) for n in field)} for x in R[:5]]
    facts = []
    if w.get("st"):
        facts.append(f"{nm.name(nm.key(w['name']))} started P{w['st']}" + (f" and led {w['led']} of {pick['laps']} laps" if w.get("led") else "") + ".")
    m = _margin(pick)
    if m is not None:
        facts.append(f"Margin of victory: {m:.3f}s.")
    led = max(R, key=lambda x: x.get("led") or 0)
    if (led.get("led") or 0) > (w.get("led") or 0):
        facts.append(f"{nm.name(nm.key(led['name']))} led the most laps ({led['led']}) but finished P{led['fin']}.")
    back = [x for x in top5 if x["now"]]
    if back:
        facts.append("Still racing in QSR today: " + ", ".join(x["driver"] for x in back[:3]) + ".")
    ago = "Last year" if years == 1 else (f"{years} years ago" if years > 1 else "Earlier this year")
    return {"id": pick["id"], "date": pick["date"], "ago": ago, "track": pick["track"], "series": sn.get(pick["series"], pick["series"]),
            "laps": pick.get("laps"), "field": len(R), "winner": nm.name(nm.key(w["name"])), "top5": top5, "facts": facts,
            "this_week_track": bool(at)}

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
        c["titles"] = []
        t["titles"] = 0
        c["totals"] = t
    # championships + records from the record book
    for ch in hist.get("champions") or []:
        c = _match(out, ch.get("driver")) or _match(out, ch.get("listed_as"))
        if c is None:  # a champion from a series with no Sim Racer Hub stats: still gets a career entry
            k = norm(ch.get("driver"))
            c = out.setdefault(k, {"key": k, "name": pretty_base(ch.get("driver")), "names": [ch.get("driver")], "lines": [],
                                   "titles": [], "totals": {"series": 0, "starts": 0, "wins": 0, "top5": 0, "top10": 0,
                                                            "top10_partial": False, "poles": 0, "avg_finish": None, "avg_start": None,
                                                            "rating": None, "win_pct": 0, "top5_pct": 0, "series_won_in": [], "titles": 0}})
        c["titles"].append(f"{ch.get('year')} {ch.get('series')}")
        c["totals"]["titles"] = len(c["titles"])
    for rec in hist.get("records") or []:
        c = _match(out, rec.get("driver"))
        if c is not None:
            c.setdefault("records", []).append(f"{rec.get('record')}: {rec.get('value')} ({rec.get('year')})")
    return out


def _match(cs, name):
    if not name:
        return None
    q = norm(name)
    for c in cs.values():
        if q == c["key"] or q in {norm(n) for n in c["names"]} or base(q) == base(c["key"]):
            return c
    return None


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
         "win_pct": ("win_pct", True), "series": ("series", True), "titles": ("titles", True)}


def leaders(cs, stat="wins", n=10, min_starts=0):
    field, desc = STATS.get(stat, (stat, True))
    rows = [c for c in cs.values() if c["totals"]["starts"] >= min_starts and c["totals"].get(field) is not None]
    rows.sort(key=lambda c: ((-c["totals"][field]) if desc else c["totals"][field], -c["totals"]["wins"], -c["totals"]["starts"]))
    return rows[:n]


def career_line(c):
    t = c["totals"]
    parts = ([f"{t['titles']} championship{'s' if t['titles'] != 1 else ''} ({', '.join(c.get('titles', []))})"] if t.get("titles") else []) + [
             f"{t['starts']} starts in {t['series']} series", f"{t['wins']} wins", f"{t['poles']} poles",
             f"{t['top5']} top 5s", f"{t['top10']}{'+' if t['top10_partial'] else ''} top 10s"]
    if t["avg_finish"] is not None:
        parts.append(f"avg finish {t['avg_finish']}")
    if t["rating"] is not None:
        parts.append(f"avg SRH rating {t['rating']}")
    for r in c.get("records") or []:
        parts.append(f"record: {r}")
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
    champs = hist.get("champions") or []
    if champs:
        out.append("CHAMPIONS (QSR record book): " + "; ".join(f"{x.get('year')} {x.get('series')}: {pretty_base(x.get('driver'))}"
                                                                    + (f" (known on Discord as {x['listed_as']})" if x.get("listed_as") else "") for x in champs))
    board("MOST CHAMPIONSHIPS", "titles", 5)
    recs = hist.get("records") or []
    if recs:
        out.append("RECORDS: " + "; ".join(f"{r.get('record')}: {r.get('value')} by {pretty_base(r.get('driver'))} ({r.get('year')})" for r in recs))
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
    if hist.get("races"):
        try:
            out.extend(_race_context(hist, data, cs, question, people[:4]))
        except Exception as e:  # never let the extras break Dale
            out.append(f"(race-by-race stats unavailable: {e})")
    return "\n".join(out)


def _race_context(hist, data, cs, question, people):
    out = []
    cov = hist.get("race_coverage") or {}
    sn = _series_names(hist, data)
    have = ", ".join(f"{sn.get(k, k)} {v.get('races')}/{v.get('of')} races" for k, v in cov.items())
    out.append("RACE-BY-RACE RECORDS (from full race results: " + (have or "SRH") + ", plus every scored HHPS race; "
               "laps led, start spots and margins exist for the Sim Racer Hub races only):")
    for r in race_records(hist, data, cs):
        out.append(f"  - {r['record']}: {r['value']}. {r['detail']}")
    nx = next_race(hist, data)
    tracks = tracks_in(question)
    if nx and nx["track"] not in tracks:
        tracks.append(nx["track"])
    for t in tracks[:3]:
        tb = track_book(hist, data, t, cs)
        tag = f" (NEXT RACE: HHPS round {nx['round']}, {nx.get('date', '')})" if nx and t == nx["track"] else ""
        out.append("TRACK HISTORY" + tag + ": " + (track_line(tb) if tb else f"{t}: QSR has no race results on file there yet."))
    for c in people:
        d = driver_races(hist, data, c["name"], cs)
        if d and d.get("races"):
            out.append(driver_race_line(d))
        ms = milestones(cs, c["name"])
        if ms:
            out.append(f"MILESTONES {c['name']}: " + "; ".join(ms))
    if len(people) >= 2:
        out.append(h2h_line(head_to_head(hist, data, people[0]["name"], people[1]["name"], cs)))
    return out


def talking_stats(cs, name, hist=None, data=None, track=None):
    """Short all-time facts for the broadcast talking points. Pass hist/data
    (and the track) to add track wins and milestones."""
    c = find(cs, name)
    if not c:
        return None
    t = c["totals"]
    wins_rank = rank_of(cs, c)
    out = {"name": c["name"], "starts": t["starts"], "wins": t["wins"], "poles": t["poles"], "top5": t["top5"],
           "titles": t.get("titles", 0), "title_list": c.get("titles", []),
           "series": t["series"], "avg_finish": t["avg_finish"], "wins_rank": wins_rank if t["wins"] else None}
    if hist is not None:
        try:
            out["milestones"] = milestones(cs, name)
            if track:
                tb = track_book(hist, data, track, cs, n=99)
                me = next((x for x in (tb or {}).get("leaders", []) if x["driver"] == c["name"]), None)
                out["track"] = tb["track"] if tb else track_name(track)
                out["track_wins"] = me["wins"] if me else 0
                out["track_starts"] = me["starts"] if me else 0
                out["track_avg"] = me["avg_finish"] if me else None
        except Exception:
            pass
    return out


# ── archiving a finished QSR season ────────────────────────────────────
def next_label(label):
    m = re.match(r"^(.*?)(\d+)\s*$", str(label or "Season 1"))
    return f"{m.group(1)}{int(m.group(2)) + 1}" if m else "Season 2"


def archive_season(path, data, label=None, name=None, crown=False):
    """Freeze the live season from data.json into qsr_history.json so it stays
    in the all-time record after standings are reset. Safe to call twice."""
    hist = load(path)
    label = label or data.get("season_label") or "Season 1"
    sid = f"{CURRENT_ID}_{norm(label).replace(' ', '')}"
    rows = current_rows(data)
    if not rows:
        return False, "No races in data.json to archive."
    hist["series"] = [s for s in hist.get("series", []) if s.get("id") != sid]
    if crown:
        st = data.get("standings") or {}
        if st:
            champ = max(st.items(), key=lambda kv: (kv[1].get("points", 0), kv[1].get("wins", 0)))[0]
            title = f"{CURRENT_SHORT} {label}"
            hist["champions"] = [c for c in hist.get("champions", []) if c.get("series") != title]
            import datetime as _dt
            hist["champions"].append({"year": _dt.date.today().year, "series": title, "driver": champ})
    hist["series"].append({"id": sid, "name": name or f"{CURRENT_NAME} {label}", "short": f"HHPS {label}",
                           "source": "Race Control archive", "rows": rows,
                           "drivers": len(rows), "total_starts": sum(r["starts"] for r in rows),
                           "total_wins": sum(r["wins"] for r in rows)})
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(hist, f, indent=1)
    os.replace(tmp, path)
    return True, f"Archived {label}: {len(rows)} drivers, {sum(r['wins'] for r in rows)} races."
