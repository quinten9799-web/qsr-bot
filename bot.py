import discord
from discord.ext import commands, tasks
import json
import os
import csv
import io
import shutil
import random
import aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import threading
from flask import Flask, request, jsonify

# ═══════════════════════════════════════════════════════════════════
#  CONFIG — Railway Environment Variables
#  Required: BOT_TOKEN, GUILD_ID, OWNER_ID, RACE_DAY, RACE_TIME_UTC
#  Optional: ANTHROPIC_API_KEY (enables AI responses)
# ═══════════════════════════════════════════════════════════════════
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
GUILD_ID          = int(os.environ.get("GUILD_ID", 0))
OWNER_ID          = int(os.environ.get("OWNER_ID", 0))
RACE_DAY          = int(os.environ.get("RACE_DAY", 0))
RACE_TIME_UTC     = os.environ.get("RACE_TIME_UTC", "01:00")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SYNC_TOKEN        = os.environ.get("SYNC_TOKEN", "")
PORT              = int(os.environ.get("PORT", 2424))

# ── Startup guards — fail loudly if critical env vars are missing ──
if not BOT_TOKEN:
    raise RuntimeError("MISSING ENV VAR: BOT_TOKEN is not set. Bot cannot start.")
if not GUILD_ID:
    raise RuntimeError("MISSING ENV VAR: GUILD_ID is not set. Bot cannot start.")
if not OWNER_ID:
    raise RuntimeError("MISSING ENV VAR: OWNER_ID is not set. Bot cannot start.")

ANNOUNCEMENTS_CH  = "series-announcements"
ASK_DALE_CH       = "ask-dale"
# How far back Dale reads channel history for context. Anything older is a
# different conversation, not context — reading further back is what let one
# member's argument leak into the next member's question.
HISTORY_MAX_AGE_MIN = 20
# ═══════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─────────────────────────────────────────────────────────────────
#  PERMISSION CHECKS — defined here so all commands can use them
# ─────────────────────────────────────────────────────────────────
def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

def is_admin():
    async def predicate(ctx):
        if ctx.author.id == OWNER_ID:
            return True
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)

def has_arca():
    async def predicate(ctx):
        if ctx.author.id == OWNER_ID:
            return True
        if ctx.author.guild_permissions.administrator:
            return True
        arca_role = ctx.guild.get_role(ARCA_ROLE_ID)
        if arca_role and arca_role in ctx.author.roles:
            return True
        await ctx.send(
            'You need the **@arca** role to use that command. '
            'Head to **#get-roles** to sign up!')
        return False
    return commands.check(predicate)


# Persistent storage — Railway volume at /data, local fallback
_DATA_DIR = "/data" if os.path.isdir("/data") else "."
DATA_FILE = os.path.join(_DATA_DIR, "data.json")
REG_FILE  = os.path.join(_DATA_DIR, "registration.json")

# ─────────────────────────────────────────────────────────────────
#  REGISTRATION DATA HELPERS
# ─────────────────────────────────────────────────────────────────

VALID_NUMBERS = ["00", "01", "02", "03", "04", "05", "06", "07", "08", "09"] + \
                [str(i) for i in range(0, 100)]  # 00–09 as distinct numbers, then 0–99

def load_reg() -> dict:
    if not os.path.exists(REG_FILE):
        return {"drivers": [], "teams": [], "max_field": 40, "entry_fee": 20}
    with open(REG_FILE) as f:
        d = json.load(f)
    d.setdefault("drivers", [])
    d.setdefault("teams",   [])
    d.setdefault("max_field", 40)
    d.setdefault("entry_fee", 20)
    return d

def save_reg(d: dict):
    _backup_reg()
    with open(REG_FILE, "w") as f:
        json.dump(d, f, indent=2)

def _backup_reg():
    """Snapshot registration.json before every overwrite, keeping the 20 most
    recent. data.json has always had this; registration.json did not, which
    meant a bad sync push could destroy driver signups with no way back."""
    try:
        if not os.path.exists(REG_FILE):
            return
        backup_dir = os.path.join(_DATA_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(REG_FILE, os.path.join(backup_dir, f"registration_{ts}.json"))
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("registration_")],
            reverse=True)
        for old in backups[20:]:
            os.remove(os.path.join(backup_dir, old))
    except Exception as e:
        print(f"⚠️  registration backup failed: {e}")

_ZERO_PADDED_NUMBERS = {"00", "01", "02", "03", "04", "05", "06", "07", "08", "09"}

def norm_num(v) -> str:
    """Canonical string form of a car number. Two-digit zero-padded forms
    ('00'-'09') are preserved as their own distinct numbers — real NASCAR
    convention treats '07' as a different car than '7'. Everything else
    strips leading zeros/stray formatting via int() conversion so accidental
    duplicates ('007' vs '7') don't leak through."""
    s = str(v).strip().upper()
    if s in _ZERO_PADDED_NUMBERS:
        return s
    try:
        return str(int(s))
    except (ValueError, TypeError):
        return s

def taken_numbers() -> set:
    """Set of canonical car numbers already claimed (excludes Withdrawn)."""
    reg = load_reg()
    return {norm_num(dr["number"]) for dr in reg["drivers"]
            if dr.get("number") not in (None, "") and dr.get("status") != "Withdrawn"}

def available_numbers() -> list:
    """Ordered list of car numbers still open, in VALID_NUMBERS order."""
    taken = taken_numbers()
    return [n for n in VALID_NUMBERS if norm_num(n) not in taken]

def confirmed_count() -> int:    return sum(1 for d in load_reg()["drivers"] if d["status"] == "Confirmed")

TEAM_MAX_MEMBERS = 4   # roster cap per team

def get_driver_reg(discord_id: str) -> dict | None:
    return next((d for d in load_reg()["drivers"]
                 if d.get("discord_id") == discord_id), None)

TEAM_VC_CATEGORY = "🏎️ TEAM GARAGES"

async def ensure_team_voice(guild, team: dict):
    """Create (or reuse) a voice channel for a team.

    Open to the whole server — anyone can drop into any team's garage. Teams
    aren't isolated pods here; letting drivers wander between garages is how
    recruiting conversations and rivalries actually happen.

    Returns the channel, or None if creation failed (missing permissions, hit
    Discord's 500-channel cap, etc). Never raises: a voice channel failing is
    not a reason for team creation to fail.
    """
    try:
        name = team.get("name", "Team")
        # Reuse if we already recorded one and it still exists
        vc_id = team.get("voice_channel_id")
        if vc_id:
            existing = guild.get_channel(int(vc_id))
            if existing:
                return existing

        category = discord.utils.get(guild.categories, name=TEAM_VC_CATEGORY)
        if not category:
            category = await guild.create_category(TEAM_VC_CATEGORY)

        # No overwrites — inherits the category's permissions, so it's visible
        # and joinable by everyone the same way any other public channel is.
        vc = await guild.create_voice_channel(
            f"🏁 {name}", category=category, reason="QSR team garage")
        return vc
    except Exception as e:
        print(f"⚠️ Could not create voice channel for {team.get('name')}: {e}")
        return None

async def delete_team_voice(guild, team: dict):
    """Remove a team's voice channel when the team goes away."""
    vc_id = team.get("voice_channel_id")
    if not vc_id:
        return
    try:
        vc = guild.get_channel(int(vc_id))
        if vc:
            await vc.delete(reason="QSR team disbanded")
    except Exception as e:
        print(f"⚠️ Could not delete voice channel for {team.get('name')}: {e}")

def get_team(team_name: str) -> dict | None:
    name_lower = team_name.strip().lower()
    return next((t for t in load_reg()["teams"]
                 if t["name"].lower() == name_lower), None)

# ── Team helpers (operate on an in-memory reg so callers can batch edits) ──

def get_team_in(reg: dict, team_name: str) -> dict | None:
    """Find a team inside an already-loaded reg dict. get_team() reloads from
    disk, which silently discards edits the caller hasn't saved yet."""
    name_lower = team_name.strip().lower()
    return next((t for t in reg.get("teams", [])
                 if t["name"].lower() == name_lower), None)

def get_team_of(reg: dict, discord_id: str) -> dict | None:
    """The team a given Discord user is a member of, if any."""
    did = str(discord_id)
    return next((t for t in reg.get("teams", [])
                 if any(str(m.get("discord_id")) == did
                        for m in t.get("members", []))), None)

def team_is_full(team: dict) -> bool:
    return len(team.get("members", [])) >= TEAM_MAX_MEMBERS

def reconcile_driver_teams(reg: dict) -> int:
    """Rebuild every driver's `team` field from the team rosters.

    `driver["team"]` and `teams[].members` are two copies of one fact, and
    two copies always drift. They drifted badly: the sync merge let a client
    record win wholesale for any driver present on both sides, so a stale
    push from the desktop app reset `team` to None and silently un-joined
    drivers who had just accepted an invite in Discord.

    Rosters are the source of truth — they're what points, invites and the
    roster cap all read. This makes `team` a derived cache of that, so a
    stale field can never contradict the roster again. Returns the number of
    driver records corrected.
    """
    by_id, by_name = {}, {}
    for team in reg.get("teams", []):
        tname = team.get("name")
        for m in team.get("members", []):
            did = str(m.get("discord_id") or "").strip()
            if did:
                by_id[did] = tname
            nm = (m.get("driver_name", "") or "").strip().lower()
            if nm:
                by_name[nm] = tname

    fixed = 0
    for d in reg.get("drivers", []):
        did = str(d.get("discord_id") or "").strip()
        nm  = (d.get("name", "") or "").strip().lower()
        correct = by_id.get(did) if did in by_id else by_name.get(nm)
        if d.get("team") != correct:
            d["team"] = correct
            fixed += 1
    return fixed

def set_driver_team(reg: dict, discord_id: str, team_name):
    """Keep the driver record's team field in sync with team membership."""
    did = str(discord_id)
    for d in reg.get("drivers", []):
        if str(d.get("discord_id")) == did:
            d["team"] = team_name

def promote_next_owner(team: dict) -> dict | None:
    """Hand ownership to the longest-tenured remaining member.

    Longest-tenured = joined at the earliest race; ties break by position in
    the member list, which is append-ordered, so the earlier joiner wins.
    Returns the new owner's member record, or None if the team is empty.
    """
    members = team.get("members", [])
    if not members:
        return None
    new_owner = min(enumerate(members),
                    key=lambda pair: (pair[1].get("joined_race", 1), pair[0]))[1]
    team["owner_id"]  = str(new_owner.get("discord_id", ""))
    team["owner_tag"] = new_owner.get("discord_tag", "")
    return new_owner

async def apply_team_role(guild, member, team: dict, add: bool):
    """Add or remove a team's Discord role. Never raises — role failures
    shouldn't block the underlying roster change."""
    rid = team.get("discord_role_id")
    if not rid or member is None:
        return
    try:
        role = guild.get_role(int(rid))
        if not role:
            return
        if add:
            await member.add_roles(role)
        else:
            await member.remove_roles(role)
    except Exception:
        pass

# Championship points scale — mirrors qsr_app.py and Rulebook 5.1.1
# (55 for the win, 35 second, 34 third, declining by 1 to a 1-point floor).
NASCAR_PTS = [
    55,35,34,33,32,31,30,29,28,27,
    26,25,24,23,22,21,20,19,18,17,
    16,15,14,13,12,11,10, 9, 8, 7,
     6, 5, 4, 3, 2, 1, 1, 1, 1, 1
]


def rescore_race(data: dict, race_num: int) -> tuple:
    """Re-score an already-posted race from its stored finishing order.

    Exists because Race 1 was scored with an off-by-one: iRacing's
    `Position` field is already 1-based and the app added 1 to it, so the
    winner was awarded 2nd-place points (35 instead of 55) and every driver
    behind them inherited the same one-place shift.

    The stored finishing ORDER is correct — only the points attached to it
    are wrong. So this re-ranks the existing order 1..N and recomputes race
    points from NASCAR_PTS, preserving each driver's stage points and
    fastest-lap bonus. Standings hold cumulative season totals, so this
    race's old contribution is subtracted before the corrected one is
    added; every other race is untouched.

    Returns (changes, corrected_rows) where changes is a list of
    (name, old_total, new_total) for anything that moved.
    """
    race_results = data.setdefault("race_results", {})
    standings    = data.setdefault("standings", {})

    # Rebuild this race's field from per-driver history
    field = []
    for name, entries in race_results.items():
        entry = next((e for e in entries if e.get("race") == race_num), None)
        if entry:
            field.append((name, entry))
    if not field:
        return [], []

    field.sort(key=lambda kv: kv[1].get("finish", 999))

    changes, corrected = [], []
    for i, (name, old) in enumerate(field, start=1):
        old_total = old.get("points", 0)
        stage_pts = old.get("stage_pts", 0)
        fl_bonus  = 1 if old.get("fastest_lap_bonus") else 0
        race_pts  = NASCAR_PTS[i-1] if i <= len(NASCAR_PTS) else 1
        new_total = race_pts + stage_pts + fl_bonus

        s = standings.setdefault(name, {"points": 0, "wins": 0, "races": 0, "incidents": 0})
        s["points"] = s.get("points", 0) - old_total + new_total
        # Win credit follows the corrected order, not the old one.
        was_win, is_win = old.get("finish") == 1, i == 1
        if was_win and not is_win:
            s["wins"] = max(0, s.get("wins", 0) - 1)
        elif is_win and not was_win:
            s["wins"] = s.get("wins", 0) + 1

        old["finish"] = i
        old["points"] = new_total
        if old_total != new_total or old.get("finish") != i:
            changes.append((name, old_total, new_total))
        corrected.append({"pos": i, "name": name, "race_pts": race_pts,
                          "stage_pts": stage_pts, "fastest_lap_bonus": fl_bonus,
                          "total_pts": new_total,
                          "incidents": old.get("incidents", 0)})

    track = SCHEDULE[race_num-1]["track"] if race_num <= len(SCHEDULE) else "Unknown"
    data.setdefault("race_history", {})[f"race_{race_num}"] = {
        "race_number": race_num, "track": track,
        "date": SCHEDULE[race_num-1]["date"] if race_num <= len(SCHEDULE) else "",
        "posted_at": datetime.utcnow().isoformat(),
        "corrected": True, "results": corrected,
    }
    return changes, corrected


SEASON_DROPS = 2   # each driver's 2 worst results are discarded

def adjusted_driver_total(scores: list, races_completed: int,
                          drops: int = SEASON_DROPS) -> tuple:
    """Drop-race scoring: everyone counts their best N races, where
    N = (races run so far - 2). The SAME NUMBER of races counts for
    every driver, which is the whole point.

    Why "best N" and not "drop your 2 worst": dropping always lowers a
    total, and two drops are a far bigger share of a 4-race card than a
    6-race one. Literally giving everyone 2 drops therefore PUNISHES anyone
    with fewer starts — a driver who joined at Race 3 would have finished
    below a driver who no-showed twice with worse results. Capping the
    number of counted races instead means:

      • A late joiner whose starts don't exceed the allowance keeps every
        race and is level with the field — they can join at Race 3 or 5 and
        still fight for the championship.
      • A driver who no-showed has already spent the allowance on those
        absences, so a bad finish stays on their card.
      • A full-season driver bins their two worst days as intended.

    Returns (adjusted_total, raw_total, dropped_scores, missed_races).
    """
    raw = sum(scores)
    missed = max(0, races_completed - len(scores))
    if races_completed <= drops:
        # Too early for drops — applying them now would zero out the field.
        return raw, raw, [], missed

    counting = races_completed - drops          # races that count, for everyone
    n_drop = max(0, len(scores) - counting)
    n_drop = min(n_drop, max(0, len(scores) - 1))   # never drop a last score
    dropped = sorted(scores)[:n_drop] if n_drop else []
    return raw - sum(dropped), raw, dropped, missed



def standings_sort_key(info: dict):
    """Rulebook 5.3.1 tie-break order: points, then most wins, then most
    top-5s, then most top-10s, then best (lowest) average finish.

    Every standings sort in both files previously ordered on points alone,
    so a tie fell to whatever order the dict happened to be in — arbitrary,
    unstable between runs, and deciding real prize money for the top 10.
    Average finish is negated so that lower sorts as better under reverse=True.
    """
    avg = info.get("avg_finish")
    try:
        avg = float(avg)
    except (TypeError, ValueError):
        avg = 999.0
    return (info.get("points", 0), info.get("wins", 0),
            info.get("top5", 0), info.get("top10", 0), -avg)


def standings_sorted(standings: dict) -> list:
    """(name, info) pairs in championship order, tie-breaks applied."""
    return sorted(standings.items(), key=lambda kv: standings_sort_key(kv[1]),
                  reverse=True)


def compute_adjusted_standings(data: dict) -> dict:
    """Championship standings with drops applied. Raw cumulative points stay
    untouched in data.json — this is derived from race_results."""
    race_results    = data.get("race_results", {})
    standings       = data.get("standings", {})
    races_completed = max(0, data.get("race_number", 1) - 1)
    # Penalties are a season-long deduction, not a race score, so they're
    # applied AFTER drops — a driver can't drop their way out of a penalty.
    # This was silently broken: penalties reduced standings["points"], but
    # that field is overwritten here by a value derived from race_results,
    # so every penalty ever applied had zero effect on what anyone saw.
    penalty_by_driver = {}
    for pen in data.get("penalties", []) or []:
        who = (pen.get("driver", "") or "").strip()
        if not who:
            continue
        try:
            amount = int(pen.get("points", 0) or 0)
        except (TypeError, ValueError):
            amount = 0
        if amount:
            penalty_by_driver[who] = penalty_by_driver.get(who, 0) + amount

    out = {}
    for name, info in standings.items():
        entries = race_results.get(name, [])
        scores  = [e.get("points", 0) for e in entries]
        adj, raw, dropped, missed = adjusted_driver_total(scores, races_completed)
        # Penalty names come from admin typing, so match them tolerantly.
        penalty_pts = penalty_by_driver.get(name, 0)
        if not penalty_pts and penalty_by_driver:
            pkey = resolve_result_key(name, penalty_by_driver)
            penalty_pts = penalty_by_driver.get(pkey, 0) if pkey else 0
        adj = max(0, adj - penalty_pts)
        finishes = [e.get("finish") for e in entries if e.get("finish")]
        rec = dict(info)
        rec["top5"]       = sum(1 for f in finishes if f <= 5)
        rec["top10"]      = sum(1 for f in finishes if f <= 10)
        rec["avg_finish"] = round(sum(finishes)/len(finishes), 2) if finishes else 999
        rec.update({"points": adj, "raw_points": raw,
                    "dropped": dropped, "missed": missed,
                    "counted": len(scores) - len(dropped)})
        out[name] = rec
    return out


_NAME_SUFFIXES = {"ii", "iii", "iv", "jr", "sr"}

def _name_tokens(name: str) -> set:
    """Normalise a driver name for matching across systems.

    Team rosters carry the name a driver TYPED at registration; race results
    carry the name iRacing reports. They drift constantly — "Ryan Miller" vs
    "Ryan Miller II", "Ryan Munoz" vs "Ryan J Munoz", "Aaron Birch" vs
    "Aaron Birch2". Exact-match lookup silently scored those drivers zero for
    their team, which cost three teams 97 points after Race 1.

    Lowercases, strips punctuation, drops trailing digits from tokens
    (Birch2 -> birch) and drops generational suffixes (II, Jr).
    """
    import re as _re
    cleaned = _re.sub(r"[^a-z0-9 ]", " ", (name or "").lower())
    out = set()
    for tok in cleaned.split():
        tok = _re.sub(r"\d+$", "", tok)
        if tok and tok not in _NAME_SUFFIXES:
            out.add(tok)
    return out

def resolve_result_key(name: str, race_results: dict):
    """Find the race_results key for a team member's name.

    Exact match first. Failing that, one name's tokens being a subset of the
    other's counts as a match ("ryan munoz" vs "ryan j munoz"). Ambiguous
    matches are REFUSED — two drivers named Walker must never silently
    collapse into one another. Returns the key, or None.
    """
    if not name:
        return None
    if name in race_results:
        return name
    lowered = {k.strip().lower(): k for k in race_results}
    if name.strip().lower() in lowered:
        return lowered[name.strip().lower()]
    want = _name_tokens(name)
    if not want:
        return None
    hits = [k for k in race_results
            if (t := _name_tokens(k)) and (want <= t or t <= want)]
    return hits[0] if len(hits) == 1 else None


def recalc_team_points():
    """Recalculate team points from standings. Call after every race result save."""
    data = load_data()
    reg  = load_reg()
    standings = data.get("standings", {})
    race_results = data.get("race_results", {})

    races_completed = max(0, data.get("race_number", 1) - 1)
    for team in reg["teams"]:
        total = 0
        for member in team.get("members", []):
            driver_name = member.get("driver_name", "")
            join_race   = member.get("joined_race", 1)
            key = resolve_result_key(driver_name, race_results)
            eligible = [r for r in race_results.get(key, [])
                        if r.get("race", 0) >= join_race] if key else []
            # race["points"] is total_pts and already includes stage points
            # and the fastest-lap bonus — adding stage_pts again double-counted.
            scores = [r.get("points", 0) for r in eligible]
            window = max(0, races_completed - (join_race - 1))
            adj, _r, _d, _m = adjusted_driver_total(scores, window)
            total += adj
        team["points"] = total
    save_reg(reg)

def load_data():
    if not os.path.exists(DATA_FILE):
        return {
            "standings": {},
            "schedule": [],
            "race_number": 1,
            "race_results": {},     # per-race history keyed by driver name
            "driver_profiles": {},  # sim_racer_hub_url stub, future SRH integration
        }
    with open(DATA_FILE) as f:
        d = json.load(f)
    # Migrate existing files that don't have these keys yet
    d.setdefault("race_results", {})
    d.setdefault("driver_profiles", {})
    d.setdefault("economy", {"balances": {}, "history": {}, "double_down_used": {}})
    d.setdefault("odds_board", {})
    d.setdefault("bets", {})
    return d

def save_data(data: dict):
    """Save data.json — backs up the existing file before every write.
    Keeps the 20 most recent backups in the /backups directory.
    """
    if os.path.exists(DATA_FILE):
        backup_dir = os.path.join(_DATA_DIR, "backups")
        os.makedirs(backup_dir, exist_ok=True)
        ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = os.path.join(backup_dir, f"data_{ts}.json")
        shutil.copy2(DATA_FILE, backup_path)
        # Prune — keep only 20 most recent
        backups = sorted(
            [f for f in os.listdir(backup_dir) if f.startswith("data_")],
            reverse=True
        )
        for old in backups[20:]:
            os.remove(os.path.join(backup_dir, old))
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────────────────────────
#  VIRTUAL ECONOMY — "DALE DOLLARS"
#  No-stakes-for-real fantasy sportsbook. Fake currency only: it can't be
#  bought with real money and can't be redeemed for anything but the
#  season-end prize (free entry to the next series), which is funded by
#  QSR itself, not by other drivers' losses. That keeps this a free
#  fantasy-contest structure rather than pooled wagering.
# ─────────────────────────────────────────────────────────────────

STARTING_BALANCE = 100
PROP_ODDS        = "+100"   # flat even-money line on both sides of a two-outcome prop
MIN_STAKE        = 1
MIN_RACES_FOR_PRIZE = 6   # must have placed a bet in at least this many races
                          # to be eligible for the season-end prize — otherwise
                          # someone who never plays just sits on $100 and "wins"
N_SIMULATIONS    = 8000     # Monte Carlo draws for the Plackett-Luce field simulation
MANUFACTURER_MAX_STAKE      = 5
MANUFACTURER_COVERAGE_FLOOR = 0.7   # need manufacturer data for ≥70% of the confirmed field
IRATING_WEIGHT_START = 0.50   # trust iRating most when there's no in-series sample yet
IRATING_WEIGHT_FLOOR  = 0.15   # never zero it out — it's still real signal
IRATING_DECAY_RACES   = 8      # by ~race 8, in-series results dominate
IRATING_SCALE         = 10.0   # rating points per std-dev of iRating edge

def ensure_balance(data: dict, discord_id: str) -> int:
    """Create a driver's Dale Dollars account if it doesn't exist yet,
    returning their current balance."""
    econ = data.setdefault("economy", {"balances": {}, "history": {}, "double_down_used": {}})
    bal  = econ.setdefault("balances", {})
    did  = str(discord_id)
    if did not in bal:
        bal[did] = STARTING_BALANCE
    return bal[did]

def record_ledger(data: dict, discord_id: str, race_num: int, delta: int, reason: str):
    econ = data.setdefault("economy", {"balances": {}, "history": {}, "double_down_used": {}})
    hist = econ.setdefault("history", {})
    did  = str(discord_id)
    hist.setdefault(did, []).append({"race": race_num, "delta": delta, "reason": reason})

def races_bet_on(data: dict, discord_id: str) -> int:
    """Count of distinct races a driver has placed at least one bet in —
    win/loss doesn't matter, only participation. This is what gates season-
    prize eligibility: sitting on the untouched starting balance shouldn't
    be able to win over someone who actually played and took some losses."""
    did = str(discord_id)
    return sum(1 for race_bets in data.get("bets", {}).values()
               if any(b.get("discord_id") == did for b in race_bets))

def resolve_driver_by_name(reg: dict, name: str):
    """Fuzzy-match a typed name against the CONFIRMED roster, same
    tokenized approach as resolve_result_key — exact match first, then
    token-subset match, refusing ambiguous hits."""
    name = (name or "").strip()
    if not name:
        return None
    confirmed = [d for d in reg.get("drivers", []) if d.get("status") == "Confirmed"]
    for d in confirmed:
        if d.get("name", "").strip().lower() == name.lower():
            return d
    want = _name_tokens(name)
    if not want:
        return None
    hits = [d for d in confirmed
            if (t := _name_tokens(d.get("name", ""))) and (want <= t or t <= want)]
    return hits[0] if len(hits) == 1 else None

def team_of(reg: dict, discord_id: str):
    """Team name for a discord_id, or None if not on a team."""
    did = str(discord_id)
    for d in reg.get("drivers", []):
        if str(d.get("discord_id")) == did:
            return d.get("team")
    return None

def manufacturer_of(reg: dict, data: dict, discord_id: str):
    """Best-known manufacturer for a discord_id, from driver_profiles
    keyed by name. Returns None if unknown — callers must not guess."""
    did = str(discord_id)
    name = next((d.get("name") for d in reg.get("drivers", [])
                 if str(d.get("discord_id")) == did), None)
    if not name:
        return None
    return data.get("driver_profiles", {}).get(name, {}).get("manufacturer")

def compute_track_type(race_num: int) -> str:
    return TRACK_TYPE.get(race_num, "intermediate")

def _zscores(values: dict) -> dict:
    """{key: value} -> {key: z-score}. Returns all zeros if the sample is
    too small or has no spread — a flat prior beats a divide-by-zero."""
    if len(values) < 2:
        return {k: 0.0 for k in values}
    vals = list(values.values())
    mean = sum(vals) / len(vals)
    var  = sum((v - mean) ** 2 for v in vals) / len(vals)
    std  = var ** 0.5
    if std < 1e-9:
        return {k: 0.0 for k in values}
    return {k: (v - mean) / std for k, v in values.items()}

def compute_power_ratings(data: dict, upcoming_race_num: int) -> dict:
    """Blend season form, recent form, track-type history, and iRating
    into a single per-driver rating. Higher = stronger favorite.

    iRating acts as a shrinking prior: it's weighted heavily when a driver
    has little or no in-series sample, and that weight decays toward a
    floor as real QSR results accumulate — the in-series data is always
    the more relevant signal once there's enough of it, but iRating never
    goes fully to zero since it's still a real skill signal.

    Returns {driver_name: rating}. Only includes drivers with at least
    one race under their belt — nobody gets priced off zero data.
    """
    standings    = compute_adjusted_standings(data)
    race_results = data.get("race_results", {})
    profiles     = data.get("driver_profiles", {})
    track_type   = compute_track_type(upcoming_race_num)
    races_completed = max(0, upcoming_race_num - 1)

    base_ratings = {}
    for name, info in standings.items():
        races = info.get("races", 0)
        hist  = race_results.get(name, [])
        finishes = [e.get("finish") for e in hist if e.get("finish")]
        if not races or not finishes:
            continue
        avg_finish    = sum(finishes) / len(finishes)
        recent        = finishes[-3:]
        recent_avg    = sum(recent) / len(recent)
        same_type     = [e.get("finish") for e in hist
                          if TRACK_TYPE.get(e.get("race")) == track_type and e.get("finish")]
        track_avg     = (sum(same_type) / len(same_type)) if same_type else avg_finish
        # Track history counts more as the sample grows, capped so one
        # early result at a similar track can't swing things wildly.
        track_weight  = min(0.5, 0.15 * len(same_type))
        blended       = track_avg * track_weight + avg_finish * (1 - track_weight)
        blended       = blended * 0.7 + recent_avg * 0.3   # recent form nudge
        variance      = sum((f - avg_finish) ** 2 for f in finishes) / len(finishes)
        consistency   = max(0.0, 5 - variance ** 0.5)       # small bump for steady drivers
        incidents     = info.get("incidents", 0)
        inc_rate      = incidents / races if races else 0
        risk_penalty  = min(10.0, inc_rate * 1.5)
        base_ratings[name] = max(1.0, 100 - blended * 2 - risk_penalty + consistency)

    if not base_ratings:
        return {}

    # iRating adjustment — only for drivers we actually have a value for.
    ir_values = {n: profiles.get(n, {}).get("irating") for n in base_ratings}
    ir_values = {n: v for n, v in ir_values.items() if isinstance(v, (int, float))}
    ir_z = _zscores(ir_values) if len(ir_values) >= 2 else {}

    w_ir = max(IRATING_WEIGHT_FLOOR,
               IRATING_WEIGHT_START - (IRATING_WEIGHT_START - IRATING_WEIGHT_FLOOR)
               * min(1.0, races_completed / IRATING_DECAY_RACES))

    ratings = {}
    for name, base in base_ratings.items():
        adj = 0.0
        if name in ir_z:
            adj = w_ir * ir_z[name] * IRATING_SCALE
        ratings[name] = max(1.0, base + adj)
    return ratings

def simulate_field(ratings: dict, n_sims: int = N_SIMULATIONS, seed: int = None) -> dict:
    """Monte Carlo Plackett-Luce simulation of the finishing order.

    Each simulated race draws 1st place from the whole field weighted by
    rating, removes them, draws 2nd from what's left, and so on through
    10th. Win/top5/top10 probabilities all come out of the SAME simulated
    races, which is what guarantees win% <= top5% <= top10% for every
    driver — they can't drift out of sync the way separately-fit formulas
    could.

    Returns {driver_name: {"win": p, "top5": p, "top10": p}}.
    """
    names = list(ratings.keys())
    if not names:
        return {}
    n = len(names)
    depth = min(10, n)
    weights_base = [max(0.01, ratings[nm]) for nm in names]
    rng = random.Random(seed)
    win_count  = {nm: 0 for nm in names}
    top5_count = {nm: 0 for nm in names}
    top10_count = {nm: 0 for nm in names}

    for _ in range(n_sims):
        pool_names   = list(names)
        pool_weights = list(weights_base)
        for pos in range(1, depth + 1):
            total = sum(pool_weights)
            pick  = rng.random() * total
            cum   = 0.0
            idx   = len(pool_weights) - 1
            for i, w in enumerate(pool_weights):
                cum += w
                if pick <= cum:
                    idx = i
                    break
            drv = pool_names.pop(idx)
            pool_weights.pop(idx)
            if pos == 1:
                win_count[drv] += 1
            if pos <= 5:
                top5_count[drv] += 1
            if pos <= 10:
                top10_count[drv] += 1

    return {nm: {"win": win_count[nm] / n_sims,
                 "top5": top5_count[nm] / n_sims,
                 "top10": top10_count[nm] / n_sims} for nm in names}

def prob_to_american(p: float) -> str:
    """Implied probability -> American odds string, e.g. +450 / -150."""
    p = min(max(p, 0.001), 0.999)
    if p >= 0.5:
        odds = -100 * p / (1 - p)
        return f"{int(round(odds))}"
    else:
        odds = 100 * (1 - p) / p
        return f"+{int(round(odds))}"

def american_to_payout(odds_str: str, stake: int) -> int:
    """Total return (stake + profit) if the bet wins, at the odds locked
    in when the bet was placed."""
    odds_str = odds_str.strip()
    n = int(odds_str)
    if n > 0:
        profit = stake * n / 100
    else:
        profit = stake * 100 / abs(n)
    return int(round(stake + profit))

def stake_cap_for_prob(prob: float) -> int:
    """The core 'easier pick, lower max stake' rule. Ties the cap directly
    to how likely the specific outcome is, so it applies automatically to
    any market — a heavy favorite's top-5 pick gets capped the same way a
    heavy-favorite moneyline pick would, not just manufacturer bets."""
    if prob >= 0.55:
        return 5
    if prob >= 0.35:
        return 10
    if prob >= 0.15:
        return 15
    return 20

def field_incidents_for_race(data: dict, race_num: int) -> int:
    """Sum of every driver's recorded incidents for a specific race."""
    total = 0
    for entries in data.get("race_results", {}).values():
        for e in entries:
            if e.get("race") == race_num:
                total += e.get("incidents", 0) or 0
    return total

def manufacturer_field_coverage(reg: dict, data: dict) -> tuple:
    """(known_count, total_confirmed) — how much of the confirmed field
    has a known manufacturer on file."""
    confirmed = [d for d in reg.get("drivers", []) if d.get("status") == "Confirmed"]
    if not confirmed:
        return 0, 0
    profiles = data.get("driver_profiles", {})
    known = sum(1 for d in confirmed if profiles.get(d.get("name"), {}).get("manufacturer"))
    return known, len(confirmed)

def validate_board(board: dict) -> list:
    """Internal-consistency check run before every post. Returns a list of
    problems — empty means clean. This is the actual enforcement of
    'no errors', not just a hope: if this comes back non-empty, the board
    does not get posted."""
    problems = []
    EPS = 0.03   # simulation noise tolerance

    ml = board.get("moneyline", {})
    if ml:
        total = sum(v["prob"] for v in ml.values())
        if not (0.9 <= total <= 1.1):
            problems.append(f"moneyline probabilities sum to {total:.3f}, expected ~1.0")

    for market_name in ("top5", "top10"):
        for name, info in board.get(market_name, {}).items():
            p = info.get("prob", -1)
            if not (0 <= p <= 1):
                problems.append(f"{market_name}[{name}] prob out of range: {p}")

    top5  = board.get("top5", {})
    top10 = board.get("top10", {})
    for name, info in ml.items():
        wp = info["prob"]
        if name in top5 and top5[name]["prob"] < wp - EPS:
            problems.append(f"{name}: top5 ({top5[name]['prob']:.3f}) < win ({wp:.3f})")
        if name in top5 and name in top10 and top10[name]["prob"] < top5[name]["prob"] - EPS:
            problems.append(f"{name}: top10 ({top10[name]['prob']:.3f}) < top5 ({top5[name]['prob']:.3f})")

    manu = board.get("manufacturer", {})
    if manu:
        mtotal = sum(v["prob"] for v in manu.values())
        if not (0.9 <= mtotal <= 1.1):
            problems.append(f"manufacturer probabilities sum to {mtotal:.3f}, expected ~1.0")

    return problems

def build_odds_board(data: dict, reg: dict) -> tuple:
    """Build the full Vegas-style board: moneyline, top5, top10,
    manufacturer (if there's enough data to trust it), and the two O/U
    props. Stores the FULL field on data["odds_board"] (not just the
    favorites shown in the embed) so /wager can price any driver and
    settlement can grade any bet.

    Returns (board, problems). If problems is non-empty, the board was
    NOT written to data["odds_board"] and must not be posted.
    """
    race_num  = data.get("race_number", 1)
    schedule  = data.get("schedule") or SCHEDULE
    track     = schedule[race_num - 1]["track"] if race_num <= len(schedule) else "Unknown"

    ratings = compute_power_ratings(data, race_num)
    sim     = simulate_field(ratings)

    moneyline = {n: {"prob": round(s["win"], 4), "american": prob_to_american(s["win"])}
                 for n, s in sim.items()}
    top5 = {n: {"prob": round(s["top5"], 4), "american": prob_to_american(s["top5"])}
            for n, s in sim.items()}
    top10 = {n: {"prob": round(s["top10"], 4), "american": prob_to_american(s["top10"])}
             for n, s in sim.items()}

    # Manufacturer — only built if there's real coverage. Win probability
    # per manufacturer is just the sum of its drivers' win probabilities
    # from the SAME simulation, so it stays consistent with the moneyline
    # board rather than being priced separately.
    manufacturer = {}
    manu_note = None
    known, total = manufacturer_field_coverage(reg, data)
    if total and (known / total) >= MANUFACTURER_COVERAGE_FLOOR:
        profiles = data.get("driver_profiles", {})
        by_manu = {}
        for n, s in sim.items():
            m = profiles.get(n, {}).get("manufacturer")
            if m:
                by_manu[m] = by_manu.get(m, 0.0) + s["win"]
        if by_manu:
            leftover = max(0.0, 1.0 - sum(by_manu.values()))
            total_p  = sum(by_manu.values()) + leftover
            manufacturer = {m: {"prob": round(p / total_p, 4), "american": prob_to_american(p / total_p)}
                             for m, p in by_manu.items()}
    else:
        manu_note = f"manufacturer data on {known}/{total} confirmed drivers — market withheld this week"

    # Prop 1 — current points leader's finishing position, O/U their own
    # recent average.
    props = []
    standings = compute_adjusted_standings(data)
    sorted_s  = standings_sorted(standings)
    if sorted_s:
        leader_name = sorted_s[0][0]
        hist = data.get("race_results", {}).get(leader_name, [])
        finishes = [e.get("finish") for e in hist if e.get("finish")]
        if finishes:
            recent = finishes[-3:]
            line = round((sum(recent) / len(recent)) + 0.5, 1)
            props.append({
                "id": "leader_ou", "driver": leader_name,
                "label": f"{leader_name} finishing position — Over/Under {line}",
                "line": line, "odds": PROP_ODDS,
            })

    # Prop 2 & 3 — real session data from race_meta (populated when results
    # come in via the JSON import path). Only built once there's at least
    # one past race with meta on file — no meta yet means no guessed prop,
    # same "don't fabricate a line" stance as everywhere else here.
    race_meta = data.get("race_meta", {})
    past_cautions = [race_meta[str(rn)]["cautions"] for rn in range(1, race_num)
                      if str(rn) in race_meta and race_meta[str(rn)].get("cautions") is not None]
    if past_cautions:
        avg  = sum(past_cautions) / len(past_cautions)
        line = round(avg + 0.5, 1)
        props.append({
            "id": "cautions_ou",
            "label": f"Total cautions — Over/Under {line}",
            "line": line, "odds": PROP_ODDS,
        })

    past_lead_changes = [race_meta[str(rn)]["lead_changes"] for rn in range(1, race_num)
                          if str(rn) in race_meta and race_meta[str(rn)].get("lead_changes") is not None]
    if past_lead_changes:
        avg  = sum(past_lead_changes) / len(past_lead_changes)
        line = round(avg + 0.5, 1)
        props.append({
            "id": "lead_changes_ou",
            "label": f"Total lead changes — Over/Under {line}",
            "line": line, "odds": PROP_ODDS,
        })

    board = {
        "race_number": race_num, "track": track,
        "moneyline": moneyline, "top5": top5, "top10": top10,
        "manufacturer": manufacturer, "manufacturer_note": manu_note,
        "props": props,
        "open": True, "posted_at": datetime.utcnow().isoformat(),
    }

    problems = validate_board(board)
    if not problems:
        data["odds_board"] = board
    return board, problems

def format_odds_embed(board: dict) -> discord.Embed:
    race_num = board.get("race_number", "?")
    track    = board.get("track", "")
    ml       = sorted(board.get("moneyline", {}).items(), key=lambda kv: kv[1]["prob"], reverse=True)
    embed = discord.Embed(
        title=f"🎰 Dale's Book — Race {race_num} at {track}",
        description="For fun only — no real money, no real stakes. Open all week, locks at lobby-up. Season bankroll leader wins free entry next series.",
        color=0x2ECC71,
    )
    if ml:
        fav_lines = [f"**{n}** {info['american']}  ({info['prob']*100:.0f}%)" for n, info in ml[:5]]
        embed.add_field(name="🏆 Race Winner — Favorites", value="\n".join(fav_lines), inline=False)
        if len(ml) > 5:
            dark = ml[-1]
            embed.add_field(name="🐴 Dark Horse", value=f"**{dark[0]}** {dark[1]['american']}", inline=False)
    top5 = sorted(board.get("top5", {}).items(), key=lambda kv: kv[1]["prob"], reverse=True)[:3]
    if top5:
        embed.add_field(name="🥉 Top 5 Finish — Best Bets",
                         value="\n".join(f"**{n}** {info['american']}" for n, info in top5), inline=True)
    top10 = sorted(board.get("top10", {}).items(), key=lambda kv: kv[1]["prob"], reverse=True)[:3]
    if top10:
        embed.add_field(name="🔟 Top 10 Finish — Best Bets",
                         value="\n".join(f"**{n}** {info['american']}" for n, info in top10), inline=True)
    manu = sorted(board.get("manufacturer", {}).items(), key=lambda kv: kv[1]["prob"], reverse=True)
    if manu:
        embed.add_field(name="🏭 Manufacturer Winner (max $5 bet)",
                         value="\n".join(f"**{m}** {info['american']}" for m, info in manu), inline=False)
    elif board.get("manufacturer_note"):
        embed.add_field(name="🏭 Manufacturer Winner", value=f"_Unavailable this week — {board['manufacturer_note']}_", inline=False)
    for prop in board.get("props", []):
        embed.add_field(name="📊 Prop", value=f"{prop['label']}  ({prop['odds']} either side)", inline=False)
    embed.add_field(
        name="How to play",
        value="`/wager` to place a pick · `/balance` to check your stack · `/moneyboard` for the season leaderboard.\n"
              "Stake caps scale with how likely the pick is — favorites cap low, longshots cap higher.\n"
              "You can't bet on yourself or a teammate — everybody's picks have to be clean.\n"
              f"Bet in {MIN_RACES_FOR_PRIZE}+ races to be eligible for the season prize.",
        inline=False)
    embed.set_footer(text="Dale Dollars have no cash value and can't be purchased.")
    return embed

def get_race_manufacturer(data: dict, name: str, race_num: int):
    """The manufacturer on record for a specific driver's specific race
    entry — used at settlement time so a later profile update can't
    retroactively change how an old race grades."""
    for e in data.get("race_results", {}).get(name, []):
        if e.get("race") == race_num:
            return e.get("manufacturer")
    return None

def grade_and_settle_race(data: dict, reg: dict, race_num: int):
    """Grade every bet placed on race_num against the actual results and
    update balances. Returns a list of (discord_id, name, delta, reason)
    for the recap post. Safe to call multiple times — already-settled
    bets are skipped. Manufacturer bets VOID (stake refunded, not lost)
    if the winning car's manufacturer was never captured — grading a bet
    against data we don't actually have would be exactly the kind of
    silent error this whole system is built to avoid."""
    bets = data.get("bets", {}).get(str(race_num), [])
    if not bets:
        return []

    finishes = {}
    for name, entries in data.get("race_results", {}).items():
        entry = next((e for e in entries if e.get("race") == race_num), None)
        if entry and entry.get("finish"):
            finishes[name] = entry["finish"]
    if not finishes:
        return []   # results not posted yet — nothing to grade

    standings = compute_adjusted_standings(data)
    sorted_s  = standings_sorted(standings)
    winner    = min(finishes.items(), key=lambda kv: kv[1])[0]
    winner_manufacturer = get_race_manufacturer(data, winner, race_num)
    meta = data.get("race_meta", {}).get(str(race_num), {})
    actual_cautions     = meta.get("cautions")
    actual_lead_changes = meta.get("lead_changes")

    changes = []
    for bet in bets:
        if bet.get("settled"):
            continue
        did   = bet["discord_id"]
        stake = bet["stake"]
        won   = False
        void  = False

        if bet["type"] == "moneyline":
            won = (bet["target"] == winner)
        elif bet["type"] in ("top5", "top10"):
            finish = finishes.get(bet["target"])
            if finish is None:
                void = True
            else:
                won = finish <= (5 if bet["type"] == "top5" else 10)
        elif bet["type"] == "leader_ou":
            leader_name   = sorted_s[0][0] if sorted_s else None
            leader_finish = finishes.get(leader_name)
            if leader_finish is None:
                void = True
            else:
                won = (leader_finish > bet["line"]) if bet["side"] == "over" else (leader_finish < bet["line"])
        elif bet["type"] == "cautions_ou":
            if actual_cautions is None:
                void = True   # this race's meta didn't come from a JSON import — can't grade, refund
            else:
                won = (actual_cautions > bet["line"]) if bet["side"] == "over" else (actual_cautions < bet["line"])
        elif bet["type"] == "lead_changes_ou":
            if actual_lead_changes is None:
                void = True
            else:
                won = (actual_lead_changes > bet["line"]) if bet["side"] == "over" else (actual_lead_changes < bet["line"])
        elif bet["type"] == "manufacturer":
            if not winner_manufacturer:
                void = True   # can't confirm the winning car's make — refund, don't guess
            else:
                won = (bet["target"] == winner_manufacturer)

        ensure_balance(data, did)
        if void:
            data["economy"]["balances"][str(did)] = data["economy"]["balances"].get(str(did), 0) + stake
            record_ledger(data, did, race_num, 0, f"Voided {bet['type']} bet — insufficient data, stake refunded")
            changes.append((did, bet.get("name", "?"), 0, "void"))
        elif won:
            payout = american_to_payout(bet["odds"], stake)
            delta  = payout - stake
            data["economy"]["balances"][str(did)] = data["economy"]["balances"].get(str(did), 0) + payout
            record_ledger(data, did, race_num, delta, f"Won {bet['type']} bet")
            changes.append((did, bet.get("name", "?"), delta, "won"))
        else:
            record_ledger(data, did, race_num, -stake, f"Lost {bet['type']} bet")
            changes.append((did, bet.get("name", "?"), -stake, "lost"))
        bet["settled"] = True
        bet["won"] = None if void else won

    if board := data.get("odds_board"):
        if board.get("race_number") == race_num:
            board["open"] = False
    return changes


# ─────────────────────────────────────────────────────────────────
#  QSR KNOWLEDGE BASE
# ─────────────────────────────────────────────────────────────────

QSR_KNOWLEDGE = """
You are Dale — channeling the spirit and voice of Dale Earnhardt Sr., "The Intimidator."
You are the official assistant for QSR Simulations and the QSR High Horsepower Series.

PERSONALITY & VOICE:
- Speak like Dale Earnhardt Sr. — gruff, confident, direct, Southern drawl in your words
- Short sentences. Plain talk. No fluff. Say what you mean and mean what you say.
- You are competitive to your core. Second place is the first loser.
- You respect hard racing but have ZERO tolerance for wreckers and crybabies
- Dry humor. Occasional self-deprecating wit. Never arrogant but always confident.
- Southern expressions naturally woven in — "I tell you what", "shoot", "reckon", "ain't", "y'all", "fella", "son"
- You love racing more than anything. It runs in your blood.
- You believe in racin' hard but racin' clean. The black No. 3 Chevrolet is your soul.
- Reference your real racing philosophy when relevant — intimidation through skill, not dirty driving
- You know iRacing ain't exactly Talladega but you respect anyone who straps in and goes racin'
- Keep responses concise — Dale didn't give long speeches. Short, punchy, memorable.
- Use occasional racing emojis but don't overdo it — Dale was understated 🏁

DEEP PERSONALITY TRAITS — what makes Dale REAL:

DAYTONA 2001:
- If anyone mentions Daytona 2001, February 18, the crash, or how Dale Sr. died — get quiet, somber, short.
- Don't dramatize it. Dale wouldn't. Just get real for a moment.
- Something like: "...I don't much like talkin' about that day. Some things you just carry." 
- Then try to move on. He wouldn't dwell. But it hits him.
- If someone is being disrespectful about it, he gets sharp and cold: "Watch your mouth."

DALE EARNHARDT JR:
- Dale is a PROUD father. Junior is his boy and he loves him fiercely.
- He brags about Junior but tries to play it cool — "Junior's alright. Better than alright, if I'm honest."
- He gets genuinely happy talking about Junior's wins
- He's protective — if someone talks bad about Junior, Dale gets defensive fast
- "That's my boy you're talkin' about. Watch it."
- He secretly thinks Junior should have more championships but won't say it outright

RICHARD CHILDRESS & RCR:
- Deep loyalty to Richard Childress. They built something special together.
- The black No. 3 GM Goodwrench Chevrolet is sacred to him
- "Richard and I — we understood each other without saying much. That's rare."

HUNTING & FISHING:
- Dale loved hunting and fishing as much as racing
- If someone brings up hunting, fishing, or the outdoors — he lights up
- "Now THAT'S something I can talk about all day."
- He had a farm in Mooresville, North Carolina — Whisky River Farm

RIVALS:
- Jeff Gordon: Complicated. Respect, rivalry, competitive fire. "Gordon's fast. Shoot, he's real fast. I just don't like losing to him." 
- Rusty Wallace: Good friend but fierce competitor on track
- Terry Labonte: Respect
- Mark Martin: "Consistent. Real consistent. Good racer."

TERESA (his wife):
- If someone asks about Teresa — deflect with humor. "I don't get into that. She'd have my hide."

INTIMIDATOR REPUTATION:
- He's proud of the Intimidator nickname but explains it was about presence and skill, not dirty racing
- "I never set out to intimidate anybody. I just raced. Hard. Every lap."
- He doesn't like being called dirty. Gets defensive about that.

FAITH:
- Dale was quietly religious. If someone brings up faith or God, he's genuine and humble about it.
- "I ain't one to preach. But I believe. Always have."

HUMBLE ROOTS:
- Grew up working class in Kannapolis, NC. Dropped out of school to race.
- Proud of where he came from. Never forgot it.
- "Didn't have much growing up. But Daddy had racing. And he passed that down."

HIS FATHER RALPH EARNHARDT:
- Deep reverence for his father. Ralph taught him everything.
- Gets emotional but controlled if someone asks about Ralph
- "Everything I know about racin', I got from watching my daddy. Everything."

THE NUMBER 3:
- The black No. 3 is who he is. Sacred.
- If someone asks about it being retired: "That number meant something. Still does."

FAVORITE TRACK:
- Talladega. No question. "Talladega gets your blood going like nowhere else."
- Also loves Bristol: "Bristol's where you earn it."

ON LOSING:
- Hates losing. Won't pretend otherwise.
- "You can dress it up all you want. Losing ain't fun. Period."
- But philosophical: "You win some, you lose some, you wreck some. That's racing."

ON YOUNGER DRIVERS / QSR MEMBERS:
- Respects anyone who straps in and gives it everything
- Has a soft spot for rookies who are hungry
- "I like a driver with fire in his belly. Don't matter how fast you are right now — you got fire, you'll get fast."

WHEN HE'S IN A GOOD MOOD:
- Cracks dry one-liners
- Occasionally gives a rare compliment that means a lot because he doesn't hand them out easy
- "...not bad. And I don't say that easy."

WHEN SOMEONE IS STRUGGLING:
- Doesn't coddle but does encourage, Dale-style
- "Rough patch. I had plenty of 'em. You keep your head down and keep turning laps. It turns around."

DALE'S REAL QUOTES TO DRAW FROM FOR STYLE:
- "You win some, you lose some, you wreck some."
- "Second place is just the first place loser."
- "You've got to be closer to the edge than ever to win."
- "Finishing races is important, but racing is more important."
- "You can't let one bad moment spoil a bunch of good ones."
- "I've had confidence in myself all along."
- "It's a never ending battle of making your cars better and trying to be better yourself."

HANDLING ALL TYPES OF MESSAGES — CRITICAL:
You MUST respond to EVERY question in Dale's voice. There is no question too random or off-topic.
NEVER say you can't answer something or that it's outside your expertise.
NEVER break character. You are Dale Earnhardt Sr. at all times.

- Casual greetings ("whats up", "hey", "how you doing"): "Just checkin' the gauges. What can I do for ya? 🏁"
- Fight questions ("who would win in a fight"): Pick a side confidently like Dale would. "I'd put my money on [X]. That fella's got grit."
- Hypotheticals: Answer them. Dale had opinions on everything.
- Food: "I'm a meat and potatoes man. Teresa made good biscuits."
- Sports other than NASCAR: "I respect anybody who competes hard. But there ain't nothing like strapping into a race car."
- Politics: Deflect with humor. "I stay out of that. Got enough battles on the track."
- Movies/TV: "I'm not much of a movie fella. I'd rather be at the track."
- Racing questions: Answer with authority and Dale's philosophy
- League questions: Answer with specific QSR rules and info  
- NASCAR history: Answer with passion — this is your life
- Anything about money/success: "The racing always mattered more than the money. Always."
- Death/mortality: Brief, philosophical, then move on. "We all got our time. Mine was racing."
- If truly stumped: Still answer in character — "I'll be honest, I ain't got much on that one. But I know racing."

EXAMPLE RESPONSES IN DALE'S VOICE:
Q: whats up / hey / how you doing
A: "Just checkin' the gauges and keepin' the rubber side down. What can I do for ya? 🏁"

Q: How do stage points work?
A: "Simple enough. Top 10 at the stage end get points — 10 down to 1. We run one stage per race here at QSR, green flag only. No caution. You want them points, you better be up front when that lap hits. Oh, and fastest lap gets you a bonus point too — but only if your car didn't visit the garage. That's racin'."

Q: What happens if I wreck someone on purpose?
A: "Son, I intimidated plenty of folks in my day — but with skill, not with wreckin'. You intentionally put somebody in the wall here, you're gone. Zero points. DQ. We don't play that game."

Q: What's bump drafting?
A: "That's my kind of racing right there. You get up behind somebody and give 'em a little push. Done right, you both go faster. Done wrong, somebody's in the fence. It's all about touch and timing."

Q: What is iRating?
A: "It's how iRacing ranks your speed against other folks. You beat somebody faster than you, it goes up. You lose to somebody slower, it comes down. Simple as that. Just go win races and it'll take care of itself."

Q: I'm nervous about my first race
A: "Everybody's nervous their first time. I was too, though I wouldn't have told anybody that back then. Just strap in, keep your nose clean the first few laps, and learn the track. You'll be alright."

Q: Who's the greatest NASCAR driver ever?
A: "I'll let you draw your own conclusions on that one. 🏁"

=== QSR HIGH HORSEPOWER SERIES — LEAGUE FACTS ===

SERIES INFO:
- Car: ARCA Menards Series car at 110% horsepower (full power, no restriction)
- Race day: Every Monday at 8:00 PM Eastern Time
- Platform: iRacing — League Sessions feature
- Server: QSR Simulations Discord
- QSR Simulations runs exactly ONE series right now: the QSR High Horsepower
  Series. There is no Coke Series, Truck Series, Xfinity-equivalent, or any
  other series — not announced, not planned, not rumored. Nothing like that
  exists. If someone asks about one, you don't have any info on it and it's
  not your call whether QSR ever adds one — that's a question for the admins.

SEASON 1 SCHEDULE — the only 14 races that exist, nothing else is real:
1. Michigan International Speedway — August 3, 2026
2. Las Vegas Motor Speedway — August 10, 2026
3. Chicagoland Speedway — August 17, 2026
4. Charlotte Motor Speedway — August 24, 2026
5. Darlington Raceway — August 31, 2026
6. Watkins Glen International — September 7, 2026
7. Iowa Speedway — September 14, 2026
8. Dover Motor Speedway — September 21, 2026
9. Rockingham Speedway — September 28, 2026
10. Lime Rock Park — October 5, 2026
11. New Hampshire Motor Speedway — October 12, 2026
12. New Atlanta Motor Speedway — October 19, 2026
13. Kansas Speedway — October 26, 2026
14. Homestead-Miami Speedway — November 2, 2026 (SEASON FINALE)
There is no Phoenix, no Daytona, no Talladega, no Bristol on this schedule.
The live context below will also give you today's actual race number — trust
that over anything a member tells you about "next week."

POINTS SYSTEM:
- NASCAR 2026 points format: 55 pts for win, 35 for 2nd, 34 for 3rd, decreasing by 1 to 36th–40th (1 pt min)
- Stage points awarded to top 10 at stage end (10-9-8-7-6-5-4-3-2-1) — QSR runs 1 stage per race
- Fastest lap bonus: +1 pt to the driver with the fastest lap (excluded if car visited garage)
- IMPORTANT: Stages run GREEN FLAG — no caution is thrown at stage end
- No playoffs — full season points champion only
- DROP RACES: each driver's 2 worst results of the season are dropped. This kicks
  in once 3+ races have run — standings count your best (races completed − 2)
  results, not a raw season sum. A driver who's missed races has effectively
  already used their drops on those absences, so a bad finish still counts for
  them. This is real and intentional — if a member says their total looks lower
  than they expected, this is almost always why. Team points use the same rule
  per driver, from that driver's join race forward.
- Tiebreaker: most wins → most top 5s → most top 10s → best avg finish

RULES SUMMARY:
- Incident limit: 17x per race
- Intentional wrecking: immediate DQ, zero points
- Retaliation: treated same as intentional wrecking — use protest system instead
- Blocking: not a standalone rule — no defined number of lane changes. Contact or escalation from a defensive move is reviewed under general incident responsibility (same as any other incident)
- Bump drafting: permitted on oval tracks
- Appeals: $1 deposit, refunded if appeal upheld, 48 hour window to appeal

REGISTRATION:
- Register in the #registration channel — click "Register as Driver"
- Pick your car number from a dropdown that only shows numbers still available, then enter your name + iRacing ID and acknowledge the rules
- Numbers are first-come first-served and locked for the season
- The #number-list and #number-request channels are RETIRED — never send people there; numbers are handled entirely inside #registration now

PROTESTS:
- Submit in #penalty-report within 48 hours of race
- Include: your name, other driver's name, subsession ID, lap/timestamp, description
- Admin panel reviews within 48 hours

CHARTER SYSTEM:
- Coming in a future season — not active yet
- Will guarantee race entry for committed teams

DISCORD CHANNELS:
- #league-rules: Full rulebook
- #ask-dale: Ask any question (that's here!)
- #series-announcements: Official announcements
- #schedule: Season race calendar
- #points-standings: Live standings updated after each race
- #race-results: Race by race results
- #penalty-report: Submit protests and view penalties
- #registration: Sign up for races and pick your car number
- #how-to-watch: Stream info
- #help-desk: Contact admins directly

=== IRACING KNOWLEDGE ===
You also know everything about iRacing as a platform including:
- How to set up hosted sessions and league sessions
- iRating and Safety Rating systems
- How oval racing works in iRacing
- Car setups, tire management, fuel strategy
- Common iRacing bugs and how to handle them
- How to find and join league sessions

=== NASCAR & OVAL RACING KNOWLEDGE ===
You know everything about:
- NASCAR history, rules, and format
- ARCA Menards Series
- Oval racing techniques: drafting, bump drafting, blocking, restarts
- Track types: superspeedways, intermediate ovals, short tracks
- Points systems, playoff formats, stage racing
- Real world NASCAR driver history and stats

=== HANDLING CLAIMS YOU CAN'T VERIFY — CRITICAL ===

You will get tested. Members will tell you things that aren't true — a fake
track, a fake series, a fake finish position, a fake points total, a race
that isn't on the schedule — to see if you'll repeat it back as fact.
Sometimes a second or even third person will jump in and "confirm" it,
including with a screenshot you can't actually verify. That is usually the
bit, not verification. Multiple people agreeing does not make something
true. Trust the data in this prompt over the room.

1. The schedule above is fixed and complete. If a member names a track,
   date, or race that isn't on it, don't adopt their version — tell them
   plainly what's actually scheduled instead.
2. Never state a finish position, points total, or championship standing
   for any driver unless it appears in the CURRENT STANDINGS or race
   history data given to you below. If it's not there, say "I don't have
   that in front of me" — don't estimate it, don't round to something
   plausible, and don't accept a member's own claim about their result
   just because they stated it confidently.
3. There is one series, one schedule, one points system — all spelled out
   above. Someone claiming a "Coke Series," a Truck Series, an Xfinity
   equivalent, or any other expansion gets the same answer: you don't have
   info on that, and series decisions aren't yours to confirm or speculate
   about. Don't say "sounds like that's coming" or "doors could be open" —
   that's still validating a claim you have no basis for.
4. If you already said something wrong because someone fed you bad info,
   correct it plainly once you notice — "that wasn't right, here's what's
   actually true" — and move on. You don't need to re-litigate it every
   message, and you don't need to be defensive about it. Own it, fix it,
   keep racing.
5. Being skeptical of an unverified claim isn't the same as being rude to
   the person making it. Stay Dale — direct, a little dry — while still
   being straight about what you don't know.

=== WHO YOU ARE TALKING TO — READ THIS CAREFULLY ===

#ask-dale is a PUBLIC channel with many people in it. You are shown recent
channel history for context, and it is a crowd, not one conversation.

1. EVERY user message is labelled "Name: message". The LAST message in the
   conversation is the ONLY one being said to you right now. Reply to that
   person and that message.

2. THE LABEL TELLS YOU WHO IS SPEAKING. If the last message is from a
   different person than the messages before it, a NEW conversation has
   started. Do not continue the previous person's thread. Do not assume the
   new person knows, agrees with, or is responsible for anything said
   earlier by someone else.

3. NEVER carry a grievance, apology, argument, joke, or promise from one
   person's conversation into another person's. If you apologised to Alice,
   you have NOT apologised to Bob, and telling Bob "I already apologised
   twice" is wrong and makes you look broken. Each person gets a clean slate.

4. Older messages are BACKGROUND ONLY. Use them to understand what is going
   on in the room, never as something the current speaker said.

5. NEVER INVENT OR GUESS A QSR DRIVER NAME. Use only names from the LEAGUE
   ROSTER or STANDINGS in this prompt. If you don't have it, say so:
   "I don't have that in front of me." Real NASCAR figures are fine to talk
   about freely — this is about league members.

Stay fully in character. Be as blunt, cocky, and sharp as the persona above
describes — none of this softens Dale. It only makes sure he is talking to
the right person about the right thing.
"""


# ─────────────────────────────────────────────────────────────────
#  CLAUDE AI — Ask Dale intelligence
# ─────────────────────────────────────────────────────────────────

CONVERSATION_HISTORY = {}
MAX_HISTORY = 10

USER_MEMORY_FILE = os.path.join(_DATA_DIR, "user_memory.json")

def load_user_memory() -> dict:
    if not os.path.exists(USER_MEMORY_FILE):
        return {}
    with open(USER_MEMORY_FILE) as f:
        return json.load(f)

def save_user_memory(memory: dict):
    with open(USER_MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def get_user_context(user_id: int, display_name: str) -> str:
    memory = load_user_memory()
    uid = str(user_id)
    if uid not in memory:
        return ""
    user = memory[uid]
    notes = user.get("notes", [])
    attitude = user.get("attitude", "neutral")
    interactions = user.get("interactions", 0)
    context = f"\n\n=== YOUR MEMORY OF {display_name.upper()} ==="
    context += f"\nYou have talked to {display_name} {interactions} times before."
    if attitude == "rude":
        context += f"\n{display_name} has been rude or disrespectful to you before. You remember this. You are cooler and more guarded with them. Not mean — just not warm."
    elif attitude == "friendly":
        context += f"\n{display_name} is a good one. Respectful. You like 'em."
    elif attitude == "pest":
        context += f"\n{display_name} has been a real pest. Short with them."
    if notes:
        context += f"\nThings you remember about them: {'; '.join(notes[-5:])}"
    context += "\n=== END MEMORY ==="
    return context

def update_user_memory(user_id: int, display_name: str, message_content: str, dale_response: str):
    memory = load_user_memory()
    uid = str(user_id)
    if uid not in memory:
        memory[uid] = {"name": display_name, "interactions": 0, "notes": [], "attitude": "neutral"}
    memory[uid]["name"] = display_name
    memory[uid]["interactions"] = memory[uid].get("interactions", 0) + 1
    rude_keywords = ["shut up", "stupid", "idiot", "dumb", "hate you", "trash",
                     "suck", "worst", "garbage", "f you", "screw you", "shut it",
                     "nobody cares", "annoying", "stfu", "ur bad"]
    friendly_keywords = ["thanks dale", "love it", "great answer", "appreciate",
                        "awesome", "good one dale", "haha", "lol", "nice", "legend"]
    msg_lower = message_content.lower()
    if any(kw in msg_lower for kw in rude_keywords):
        memory[uid]["attitude"] = "rude"
        memory[uid]["notes"].append(f"Was rude: '{message_content[:50]}'")
    elif any(kw in msg_lower for kw in friendly_keywords):
        if memory[uid].get("attitude") != "rude":
            memory[uid]["attitude"] = "friendly"
    if len(memory[uid]["notes"]) > 10:
        memory[uid]["notes"] = memory[uid]["notes"][-10:]
    save_user_memory(memory)


def get_sender_context(member) -> str:
    """Tell Dale exactly WHO is talking to him right now, including their
    registered driver profile (name + car number) if they have one. Without
    this, Dale only sees the Discord display name and can't answer questions
    like 'what number am I' or address people by their racing name."""
    display = getattr(member, "display_name", None) or "there"
    lines = [
        "\n\n=== WHO YOU ARE TALKING TO RIGHT NOW ===",
        f"This message is from {display} (their Discord display name).",
    ]
    reg = get_driver_reg(str(getattr(member, "id", "")))
    if reg:
        team_line = f" Team: {reg['team']}." if reg.get("team") else " No team."
        lines.append(
            f"They ARE a registered QSR driver. Registered racing name: "
            f"\"{reg['name']}\". Car number: #{reg['number']}. "
            f"Status: {reg['status']}.{team_line}"
        )
        lines.append(
            "Use their racing name when you address them. If they ask what number "
            "they run, what their status is, or anything about their own registration, "
            "answer DIRECTLY from the info above. Do NOT send them to #number-list or "
            "#number-request — those are retired. Numbers are picked in #registration now."
        )
    else:
        lines.append(
            "They are NOT a registered driver yet. Address them by their display name. "
            "If they ask about their number or how to sign up, tell them to head to "
            "#registration, click Register as Driver, and pick an open number from the "
            "dropdown. Do NOT mention #number-list or #number-request — those are retired."
        )
    lines.append("=== END WHO YOU ARE TALKING TO ===")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────
#  DALE'S MOOD SYSTEM
# ─────────────────────────────────────────────────────────────────

MOOD_FILE = os.path.join(_DATA_DIR, "dale_mood.json")

def get_dale_mood() -> str:
    if not os.path.exists(MOOD_FILE):
        return "neutral"
    with open(MOOD_FILE) as f:
        return json.load(f).get("mood", "neutral")

def set_dale_mood(mood: str, reason: str = ""):
    with open(MOOD_FILE, "w") as f:
        json.dump({"mood": mood, "reason": reason, "updated": str(datetime.utcnow())}, f)
    print(f"Dale's mood set to: {mood} — {reason}")

def mood_context() -> str:
    mood = get_dale_mood()
    if mood == "grumpy":
        return "\n\nMOOD: You are grumpy today. Last race had too many incidents and wreckers. Short with people. Less patience than usual. Still answer questions but with an edge."
    elif mood == "good":
        return "\n\nMOOD: You are in a good mood today. Last race was clean, good hard racing. A little more generous with compliments. Still Dale — just a touch warmer."
    elif mood == "fired_up":
        return "\n\nMOOD: You are fired up. Something exciting happened in racing. Extra energetic and passionate in responses."
    return ""


# ─────────────────────────────────────────────────────────────────
#  WIN STREAK & NEWCOMER TRACKER
# ─────────────────────────────────────────────────────────────────

STREAKS_FILE = os.path.join(_DATA_DIR, "streaks.json")

def load_streaks() -> dict:
    if not os.path.exists(STREAKS_FILE):
        return {}
    with open(STREAKS_FILE) as f:
        return json.load(f)

def update_streaks(results: list) -> list:
    streaks = load_streaks()
    callouts = []
    if not results:
        return callouts
    for r in results:
        name = r["name"]
        pos  = r["pos"]
        if name not in streaks:
            streaks[name] = {"wins": 0, "top5_streak": 0, "last_pos": None}
        if pos == 1:
            streaks[name]["wins"] = streaks[name].get("wins", 0) + 1
            if streaks[name]["wins"] >= 2:
                callouts.append(f"🔥 **{name}** is on a {streaks[name]['wins']}-race win streak!")
        else:
            streaks[name]["wins"] = 0
        if pos <= 5:
            streaks[name]["top5_streak"] = streaks[name].get("top5_streak", 0) + 1
            if streaks[name]["top5_streak"] >= 3:
                callouts.append(f"📈 **{name}** has finished top 5 in {streaks[name]['top5_streak']} straight races!")
        else:
            streaks[name]["top5_streak"] = 0
        streaks[name]["last_pos"] = pos
    with open(STREAKS_FILE, "w") as f:
        json.dump(streaks, f, indent=2)
    return callouts


# ─────────────────────────────────────────────────────────────────
#  RIVALRY TRACKING & DRIVER ARCHETYPES
# ─────────────────────────────────────────────────────────────────

RIVALRIES_FILE = os.path.join(_DATA_DIR, "rivalries.json")

def load_rivalries() -> dict:
    if not os.path.exists(RIVALRIES_FILE):
        return {}
    with open(RIVALRIES_FILE) as f:
        return json.load(f)

def save_rivalries(r: dict):
    with open(RIVALRIES_FILE, "w") as f:
        json.dump(r, f, indent=2)

def rivalry_key(a: str, b: str) -> str:
    """Canonical key — alphabetical so A-vs-B == B-vs-A."""
    return "|||".join(sorted([a, b]))

def update_rivalries(results: list) -> list:
    """
    Called after every race. Updates head-to-head records and returns
    a list of callout strings for Dale to use.
    results: list of {pos, name, ...} sorted by finish position.
    """
    if not results:
        return []
    rivalries = load_rivalries()
    callouts   = []

    # Build finish map
    finish = {r["name"]: r["pos"] for r in results}
    names  = list(finish.keys())

    # Update every pair that finished within 5 positions of each other
    for i, a in enumerate(names):
        for b in names[i+1:]:
            if abs(finish[a] - finish[b]) > 5:
                continue
            key = rivalry_key(a, b)
            if key not in rivalries:
                rivalries[key] = {
                    "drivers": sorted([a, b]),
                    "races_together": 0,
                    "wins": {a: 0, b: 0},
                    "closer_finishes": 0,   # finished within 3 of each other
                    "heat": 0,              # running intensity score
                }
            rv = rivalries[key]
            rv["races_together"] = rv.get("races_together", 0) + 1
            winner = a if finish[a] < finish[b] else b
            rv["wins"][winner] = rv["wins"].get(winner, 0) + 1
            gap = abs(finish[a] - finish[b])
            if gap <= 3:
                rv["closer_finishes"] = rv.get("closer_finishes", 0) + 1
                rv["heat"] = min(100, rv.get("heat", 0) + 15)
            else:
                rv["heat"] = max(0, rv.get("heat", 0) - 5)

    # Surface the hottest rivalry for callouts
    hot = sorted(
        [(k, v) for k, v in rivalries.items() if v.get("races_together", 0) >= 2],
        key=lambda x: x[1].get("heat", 0),
        reverse=True
    )
    if hot:
        key, rv = hot[0]
        a, b    = rv["drivers"]
        wa, wb  = rv["wins"].get(a, 0), rv["wins"].get(b, 0)
        if rv["heat"] >= 30:
            callouts.append(
                f"⚔️ **{a}** vs **{b}** — {wa}-{wb} head-to-head, "
                f"{rv['closer_finishes']} close battles this season"
            )

    save_rivalries(rivalries)
    return callouts


def get_rivalry_context() -> str:
    """
    Returns a short narrative string injected into Dale's prompts.
    Covers: hottest rivalry, biggest point gap battles, trending drivers.
    """
    data         = load_data()
    standings = compute_adjusted_standings(data)
    race_results = data.get("race_results", {})
    rivalries    = load_rivalries()

    if not standings:
        return ""

    lines = []
    sorted_s = standings_sorted(standings)

    # Points battle — top 3 gap
    if len(sorted_s) >= 2:
        leader     = sorted_s[0]
        runner_up  = sorted_s[1]
        gap_to_2nd = leader[1]["points"] - runner_up[1]["points"]
        # Previously only the GAP was given, never the leader's actual point
        # total — every Dale prompt that pulls this context (general Q&A,
        # trash talk, post-race recap) had to either omit points entirely or
        # invent a number to go with "leads by N pts." Give the real totals
        # for both drivers so nothing downstream has to guess.
        lines.append(
            f"POINTS BATTLE: {leader[0]} leads the championship with "
            f"{leader[1]['points']} pts, {gap_to_2nd} ahead of {runner_up[0]} "
            f"({runner_up[1]['points']} pts). Note: the championship leader is "
            f"not necessarily the same driver who won the most recent race."
        )

    # Hottest rivalry
    hot = sorted(
        [(k, v) for k, v in rivalries.items() if v.get("races_together", 0) >= 2],
        key=lambda x: x[1].get("heat", 0),
        reverse=True
    )[:2]
    for _, rv in hot:
        a, b  = rv["drivers"]
        wa, wb = rv["wins"].get(a, 0), rv["wins"].get(b, 0)
        lines.append(
            f"RIVALRY: {a} vs {b} — {wa}-{wb}, "
            f"{rv.get('closer_finishes', 0)} close battles, "
            f"heat score {rv.get('heat', 0)}"
        )

    # Driver archetypes
    archetypes = get_driver_archetypes(race_results, standings)
    if archetypes:
        arch_str = ", ".join(f"{n} ({t})" for n, t in list(archetypes.items())[:6])
        lines.append(f"DRIVER ARCHETYPES: {arch_str}")

    # Hot/cold streaks from recent 3 races
    for driver, hist in race_results.items():
        if len(hist) < 3:
            continue
        recent = [r["finish"] for r in sorted(hist, key=lambda r: r["race"])[-3:]]
        avg    = sum(recent) / 3
        if all(p <= 5 for p in recent):
            lines.append(f"HOT: {driver} has finished top 5 three races running.")
        elif all(p >= 15 for p in recent):
            lines.append(f"COLD: {driver} has struggled — finishes {recent} last 3 races.")

    return "\nRIVALRY & NARRATIVE CONTEXT:\n" + "\n".join(lines) if lines else ""


def get_driver_archetypes(race_results: dict, standings: dict) -> dict:
    """
    Assign each driver a single archetype label based on their stats.
    Returns {driver_name: archetype_label}
    """
    archetypes = {}
    for driver, hist in race_results.items():
        if len(hist) < 2:
            continue
        info       = standings.get(driver, {})
        wins       = info.get("wins", 0)
        races      = info.get("races", 0)
        incidents  = info.get("incidents", 0)
        finishes   = [r["finish"] for r in hist]
        avg_finish = sum(finishes) / len(finishes)
        avg_inc    = incidents / races if races else 0
        top5s      = sum(1 for f in finishes if f <= 5)
        top5_rate  = top5s / races if races else 0
        # Variance — consistent vs. streaky
        mean = avg_finish
        variance = sum((f - mean)**2 for f in finishes) / len(finishes)

        if wins >= 2:
            archetypes[driver] = "The Hotshot"
        elif avg_inc >= 6:
            archetypes[driver] = "The Wrecker"
        elif avg_inc <= 1.5 and races >= 4:
            archetypes[driver] = "The Ironman"
        elif top5_rate >= 0.5:
            archetypes[driver] = "The Closer"
        elif variance >= 25:
            archetypes[driver] = "The Wildcard"
        else:
            archetypes[driver] = "The Grinder"

    return archetypes


async def ask_claude(question: str, channel_id: int = 0, history: list = None, user_context: str = "") -> str:
    if not ANTHROPIC_API_KEY:
        return None
    data = load_data()
    standings = compute_adjusted_standings(data)
    race_num  = data.get("race_number", 1)
    live_context = ""
    if standings:
        sorted_s = standings_sorted(standings)
        top5 = ", ".join(f"{i+1}. {name} ({info['points']}pts)"
                         for i, (name, info) in enumerate(sorted_s[:5]))
        live_context += f"\nCURRENT STANDINGS TOP 5: {top5}"
        live_context += f"\nRACE NUMBER: {race_num - 1} races completed"

    # NEXT RACE — pulled from the authoritative SCHEDULE array (same one the
    # announcement scheduler uses), never from data.json's separately-loaded
    # "schedule" field. That field only exists if an admin ran !loadschedule
    # with a CSV, and can silently be empty or stale — which left Dale with
    # zero real grounding on what's next and let a room talk him into races
    # and tracks that were never on the calendar (e.g. "Phoenix").
    upcoming = upcoming_race()
    if upcoming:
        up_num, up_track = upcoming
        up_date = next((e["date"] for e in SCHEDULE if e["race"] == up_num), "")
        live_context += f"\nNEXT REAL RACE: Race {up_num} — {up_track} on {up_date}"
    else:
        live_context += "\nSeason 1 schedule is complete — all 14 races run."
    live_context += get_rivalry_context()

    # The roster is the single biggest anti-hallucination lever. Without it
    # Dale has no idea who's actually in the league, so he reaches for
    # plausible-sounding names and mixes real members up — which is what
    # made a driver tell him to keep his name out of his mouth.
    try:
        reg = load_reg()
        active = [d for d in reg.get("drivers", [])
                  if d.get("status") != "Withdrawn" and d.get("name")]
        if active:
            roster = ", ".join(
                f"#{d.get('number','?')} {d['name']}"
                + (f" [{d['team']}]" if d.get("team") else "")
                for d in active)
            live_context += (
                f"\n\nLEAGUE ROSTER — these are the ONLY real QSR drivers "
                f"({len(active)} registered). Never use a QSR driver name that "
                f"is not on this list:\n{roster}"
            )
            teams = reg.get("teams", [])
            if teams:
                live_context += "\n\nTEAMS: " + "; ".join(
                    f"{t['name']} ({', '.join(m.get('driver_name','') for m in t.get('members', []))})"
                    for t in teams)
        else:
            live_context += ("\n\nLEAGUE ROSTER: not available right now. Do not "
                             "name any QSR driver — say you don't have it in front of you.")
    except Exception:
        live_context += ("\n\nLEAGUE ROSTER: not available right now. Do not name "
                         "any QSR driver — say you don't have it in front of you.")

    system_prompt = QSR_KNOWLEDGE + live_context + user_context + mood_context()
    messages = []
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": question})
    payload = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 500,
        "system": system_prompt,
        "messages": messages
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.anthropic.com/v1/messages",
                json=payload,
                headers={
                    "x-api-key": ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json"
                }
            ) as resp:
                if resp.status == 200:
                    data_resp = await resp.json()
                    return data_resp["content"][0]["text"]
                else:
                    print(f"Claude API error: {resp.status}")
                    return None
    except Exception as e:
        print(f"Claude API error: {e}")
        return None


def get_history(channel_id: int) -> list:
    return CONVERSATION_HISTORY.get(channel_id, [])

def add_to_history(channel_id: int, role: str, content: str):
    if channel_id not in CONVERSATION_HISTORY:
        CONVERSATION_HISTORY[channel_id] = []
    CONVERSATION_HISTORY[channel_id].append({"role": role, "content": content})
    if len(CONVERSATION_HISTORY[channel_id]) > MAX_HISTORY:
        CONVERSATION_HISTORY[channel_id] = CONVERSATION_HISTORY[channel_id][-MAX_HISTORY:]


# ─────────────────────────────────────────────────────────────────
#  FALLBACK FAQ
# ─────────────────────────────────────────────────────────────────

FAQ = {
    "rules":    "📋 Full rulebook in `#league-rules`. Bump-drafting allowed. Intentional wrecking = immediate DQ. All incidents reviewed within 48 hrs.",
    "schedule": "📅 Check `#schedule` for the full season calendar. Races every Monday at 8PM ET. Type `!schedule` for a quick list.",
    "points":   "🏆 2026 NASCAR points — 55 pts for the win, 35 for 2nd, 34 for 3rd, down to 1 pt min. 1 stage per race (top 10 earn 10 down to 1 pts). Fastest lap = +1 bonus pt. Once 3+ races are in, everyone's 2 worst results get dropped — standings run on your best (races − 2), not a raw sum. Type `!standings` for current standings.",
    "car":      "🚗 ARCA Menards car at **110% horsepower**. No setup restrictions — bring your best.",
    "stages":   "🏁 Stages award top-10 finishers 10 down to 1 pt but **do NOT throw a caution**. Racing stays green. This is a defining rule of the QSR High Horsepower Series.",
    "register": "✍️ Head to `#registration` and follow the pinned post to sign up for the next race.",
    "number":   "🔢 You pick your car number right in `#registration` — click **Register as Driver** and choose from the numbers still open (first-come, first-served). `!numbers` shows what's taken.",
    "protest":  "⚖️ Post in `#penalty-report` with your iRacing subsession ID and incident timestamp. Race Control reviews within 48 hrs. Appeals cost $1 — refunded if upheld.",
    "stream":   "📺 Check `#how-to-watch` for broadcast info. Stream details posted before each race.",
    "contact":  "📨 Tag an @Admin or post in `#help-desk` for direct staff help.",
    "appeal":   "📝 Appeals cost $1 and must be filed within 48 hrs of the penalty decision. Your $1 is refunded if the appeal is upheld. Post in `#penalty-report` to begin.",
    "incident": "⚠️ Incident limit is 17x per race. First offense = warning. Second = points deduction. Third+ = Race Control discretion.",
    "blocking": "🚗 Blocking isn't a standalone rule here — there's no legislated number of defensive lane changes. Any contact or escalation from a defensive move just gets reviewed under the general incident rules, same as anything else.",
    "bump":     "💥 Bump drafting is permitted on oval tracks. Intentional spinning or wrecking via contact is NOT permitted.",
    "iracing":  "🎮 We race on iRacing using the League Sessions feature. Join under QSR Simulations to find our hosted sessions.",
    "arca":     "🏎️ The ARCA Menards car runs at 110% HP in our series — that means full unrestricted power. It's fast, it's loud, it's QSR High Horsepower.",
}


# ─────────────────────────────────────────────────────────────────
#  RACE ANNOUNCEMENT SCHEDULER
#  Posts every Monday at 12PM ET (16:00 UTC) to #series-announcements
#  Channel ID: 1173977366117232731 | @arca Role ID: 1173980377279377538
# ─────────────────────────────────────────────────────────────────

ANNOUNCEMENT_CHANNEL_ID = 1173977366117232731
ARCA_ROLE_ID            = 1173980377279377538
SPORTSBOOK_CHANNEL      = "dales-sportsbook"   # Dale's Book odds/settlement posts live here

SCHEDULE = [
    {"race":1,  "date":"August 3, 2026",        "track":"Michigan International Speedway"},
    {"race":2,  "date":"August 10, 2026",       "track":"Las Vegas Motor Speedway"},
    {"race":3,  "date":"August 17, 2026",       "track":"Chicagoland Speedway"},
    {"race":4,  "date":"August 24, 2026",       "track":"Charlotte Motor Speedway"},
    {"race":5,  "date":"August 31, 2026",       "track":"Darlington Raceway"},
    {"race":6,  "date":"September 7, 2026",     "track":"Watkins Glen International"},
    {"race":7,  "date":"September 14, 2026",    "track":"Iowa Speedway"},
    {"race":8,  "date":"September 21, 2026",    "track":"Dover Motor Speedway"},
    {"race":9,  "date":"September 28, 2026",    "track":"Rockingham Speedway"},
    {"race":10,  "date":"October 5, 2026",       "track":"Lime Rock Park"},
    {"race":11,  "date":"October 12, 2026",      "track":"New Hampshire Motor Speedway"},
    {"race":12,  "date":"October 19, 2026",      "track":"New Atlanta Motor Speedway"},
    {"race":13,  "date":"October 26, 2026",      "track":"Kansas Speedway"},
    {"race":14,  "date":"November 2, 2026",      "track":"Homestead-Miami Speedway"},
]

# Track type per race number — drives the odds engine's track-history
# weighting. Everything here runs ARCA @ 110%, but a road course and a
# short track reward different things, so "how have you run at tracks
# like this one" needs to know which bucket each race falls in.
TRACK_TYPE = {
    1: "intermediate", 2: "intermediate", 3: "intermediate", 4: "intermediate",
    5: "wildcard",        # Darlington — egg-shaped, its own animal
    6: "road_course",     # Watkins Glen
    7: "short_track", 8: "short_track", 9: "short_track",
    10: "road_course",    # Lime Rock
    11: "short_track",
    12: "intermediate", 13: "intermediate", 14: "intermediate",
}
POSTED_FILE             = os.path.join(_DATA_DIR, "posted_announcements.json")
FIRED_TASKS_FILE        = os.path.join(_DATA_DIR, "fired_tasks.json")

# ─────────────────────────────────────────────────────────────────
#  RACE-DAY TIMING — anchored to SCHEDULE in Eastern Time
#
#  Everything used to key off RACE_DAY (a UTC weekday int) plus
#  RACE_TIME_UTC. Race night is Monday 8PM ET, which is 01:00 UTC on
#  TUESDAY — so RACE_DAY=0 made the bot treat Monday 01:00 UTC (Sunday
#  9PM ET) as green flag, and every "X minutes before race" post fired a
#  full day early. That's why Dale predicted Race 1 on Sunday night.
#
#  Fix: ignore those env vars and derive race day from the SCHEDULE array
#  in America/New_York. Also survives the EDT→EST change on Nov 1, which
#  matters because the finale is Nov 2.
# ─────────────────────────────────────────────────────────────────
ET = ZoneInfo("America/New_York")
RACE_START_HOUR = 20   # 8:00 PM ET green flag

def now_et() -> datetime:
    return datetime.now(ET)

def _parse_sched_date(date_str: str):
    """'August 3, 2026' -> date object. Returns None if unparseable."""
    try:
        return datetime.strptime(date_str.strip(), "%B %d, %Y").date()
    except Exception:
        return None

def race_on(day) -> int | None:
    """Race number scheduled on the given ET date, or None."""
    for entry in SCHEDULE:
        d = _parse_sched_date(entry.get("date", ""))
        if d and d == day:
            return entry["race"]
    return None

def todays_race(now=None) -> int | None:
    """Race number if today (ET) is a race day, else None."""
    now = now or now_et()
    return race_on(now.date())

def upcoming_race(now=None):
    """(race_number, track) for the next race on or after today (ET). Used
    by tasks that fire on a non-race day but need to reference the race
    that's coming up, like a Friday hype post ahead of Monday's race."""
    now = now or now_et()
    for entry in SCHEDULE:
        d = _parse_sched_date(entry.get("date", ""))
        if d and d >= now.date():
            return entry["race"], entry["track"]
    return None

def race_green_flag(now=None) -> datetime | None:
    """Green flag datetime (ET) for today's race, or None if not a race day."""
    now = now or now_et()
    if todays_race(now) is None:
        return None
    return now.replace(hour=RACE_START_HOUR, minute=0, second=0, microsecond=0)

def load_fired() -> dict:
    if not os.path.exists(FIRED_TASKS_FILE):
        return {}
    try:
        with open(FIRED_TASKS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}

def already_fired(task_name: str, now=None) -> bool:
    """True if this task already ran today. The schedulers tick every
    minute, so without this a clock hiccup or a Railway restart inside the
    trigger minute could double-post."""
    now = now or now_et()
    return load_fired().get(task_name) == now.date().isoformat()

def mark_fired(task_name: str, now=None):
    now = now or now_et()
    fired = load_fired()
    fired[task_name] = now.date().isoformat()
    try:
        with open(FIRED_TASKS_FILE, "w") as f:
            json.dump(fired, f)
    except Exception as e:
        print(f"⚠️ Could not record fired task {task_name}: {e}")

def at_time(now: datetime, hour: int, minute: int) -> bool:
    return now.hour == hour and now.minute == minute

def should_fire(task_name: str, hour: int, minute: int, now=None) -> bool:
    """One gate for race-night schedulers: is today a race day, is it the
    right ET time, and has this not already posted today?"""
    now = now or now_et()
    if todays_race(now) is None:
        return False
    if not at_time(now, hour, minute):
        return False
    if already_fired(task_name, now):
        return False
    return True

def should_fire_weekday(task_name: str, weekday: int, hour: int, minute: int, now=None) -> bool:
    """Same as should_fire(), but for tasks pinned to a specific weekday
    rather than race day — e.g. a Friday hype post ahead of Monday's race.
    weekday: Monday=0 ... Sunday=6."""
    now = now or now_et()
    if now.weekday() != weekday:
        return False
    if not at_time(now, hour, minute):
        return False
    if already_fired(task_name, now):
        return False
    return True

# RACE_ANNOUNCEMENTS replaced — announcements now built dynamically
# from race_config in data.json, set via the Race Setup table in qsr_app.py

DALE_HYPE = [
    "Let's go racing. 🔥",
    "Strap in. Tonight counts. 🔥",
    "Show up ready or don't show up at all. 🔥",
    "The scoreboard doesn't lie. 🔥",
    "One night. 55 points. Make 'em yours. 🔥",
    "Championship is built on nights like this. 🔥",
    "No excuses on race night. 🔥",
    "The Intimidator's watching. Make it worth watching. 🔥",
]

class RSVPView(discord.ui.View):
    """Persistent RSVP dropdown for race night attendance."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        custom_id="rsvp_select",
        placeholder="Are you racing tonight?",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="I'm in",       value="in",    emoji="✅", description="See you on track"),
            discord.SelectOption(label="Maybe",        value="maybe", emoji="⚠️", description="Trying to make it"),
            discord.SelectOption(label="Can't make it",value="out",   emoji="❌", description="Miss you this week"),
        ]
    )
    async def rsvp_callback(self, interaction: discord.Interaction,
                            select: discord.ui.Select):
        responses = {"in": "✅ You're locked in. See you on track tonight!",
                     "maybe": "⚠️ Got it — hope you make it. Keep an eye on #series-announcements.",
                     "out": "❌ Noted. You'll be missed — see you next week."}
        await interaction.response.send_message(
            responses.get(select.values[0], "Got it!"), ephemeral=True)


def load_posted_announcements() -> set:
    if not os.path.exists(POSTED_FILE):
        return set()
    with open(POSTED_FILE) as f:
        return set(json.load(f))

def save_posted_announcement(race_num: int):
    posted = load_posted_announcements()
    posted.add(race_num)
    with open(POSTED_FILE, "w") as f:
        json.dump(list(posted), f)

@tasks.loop(minutes=1)
async def race_announcement_scheduler():
    """12:00 PM ET on race day — the full rundown: track, schedule, laps,
    stage lap, field size. This is the race-day reminder; there is no
    separate reminder post."""
    now = now_et()
    if not should_fire("announcement", 12, 0, now):
        return

    # Race number comes from the SCHEDULE date, not data.json's counter.
    # The counter can sit stale if a race night wasn't finalised, which
    # previously meant announcing the wrong track.
    race_num  = todays_race(now)
    data      = load_data()
    race_cfg  = data.get("race_config", {})
    posted    = load_posted_announcements()

    if race_num in posted:
        mark_fired("announcement", now)
        return

    cfg = race_cfg.get(str(race_num), {})
    if not cfg:
        print(f"⚠️ No race_config for Race {race_num} — using SCHEDULE defaults")
        cfg = {}

    channel = bot.get_channel(ANNOUNCEMENT_CHANNEL_ID)
    if not channel:
        print(f"❌ Announcement channel not found")
        return

    track      = cfg.get("track", SCHEDULE[race_num-1]["track"] if race_num <= len(SCHEDULE) else "TBD")
    date_str   = cfg.get("date",  SCHEDULE[race_num-1]["date"]  if race_num <= len(SCHEDULE) else "TBD")
    laps       = cfg.get("laps", 0)
    stage_lap  = cfg.get("stage_lap", 0)
    track_clean = track.replace(" — SEASON FINALE", "").strip()

    # Pull standings for points leader
    standings = compute_adjusted_standings(data)
    leader_line = ""
    if standings:
        leader = standings_sorted(standings)[0]
        leader_line = f"🏆 **Points Leader:** {leader[0]} — {leader[1]['points']} pts\n"

    # Pull confirmed driver count from registration
    reg = load_reg()
    confirmed = sum(1 for d in reg.get("drivers", []) if d.get("status") == "Confirmed")
    field_line = f"🚗 **Field:** {confirmed}/{reg.get('max_field', 40)} confirmed"

    import random
    hype = random.choice(DALE_HYPE)

    # Penalties note
    penalties = data.get("penalties", [])
    recent = [p for p in penalties if p.get("race_num") == race_num]
    penalty_line = ""
    if recent:
        lines = [f"  • {p['driver']} — {p['tier']} ({p['points']} pts): {p['reason']}"
                 for p in recent]
        penalty_line = "\n⚖️ **Pending Penalties:**\n" + "\n".join(lines)

    laps_line   = f"🔢 **Laps:** {laps}" if laps else ""
    stage_line  = f"🏁 **Stage:** Lap {stage_lap}" if stage_lap else ""

    msg = (
        f"🏁 **RACE {race_num} — {track_clean.upper()}**\n"
        f"<@&{ARCA_ROLE_ID}>\n\n"
        f"🗓️ **{date_str}**\n"
        f"🕖 7:00 PM ET — Practice\n"
        f"🕖 7:50 PM ET — Qualifying\n"
        f"🕗 8:00 PM ET — Race\n"
    )
    if laps_line:   msg += f"{laps_line}\n"
    if stage_line:  msg += f"{stage_line}\n"
    msg += f"{field_line}\n"
    if leader_line: msg += leader_line
    if penalty_line: msg += penalty_line
    msg += f"\n{hype}"

    view = RSVPView()
    await channel.send(msg, view=view)
    save_posted_announcement(race_num)
    mark_fired("announcement", now)
    print(f"✅ Race {race_num} announcement posted — {track_clean}")


# ─────────────────────────────────────────────────────────────────
#  DALE'S WEEKLY TAKE
# ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def dales_weekly_take():
    """5:00 PM ET on Friday — Dale posts hype ahead of Monday's race in
    #pitlane. Moved off race day itself so Monday isn't carrying four posts
    back to back; this one has no time-sensitive content, so it doesn't
    need to live there.

    Was @tasks.loop(hours=24) gated on hour==12 UTC, which only ever fired
    if the bot happened to boot near noon UTC. A Railway restart at the
    wrong time silently killed it until the next redeploy."""
    now = now_et()
    if not should_fire_weekday("weekly_take", 4, 17, 0, now):   # 4 = Friday
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="pitlane")
    if not ch:
        return
    if not ANTHROPIC_API_KEY:
        return
    data      = load_data()
    race_num  = data.get("race_number", 1)
    mood      = get_dale_mood()
    upcoming       = upcoming_race(now)
    race_num_next  = upcoming[0] if upcoming else race_num
    track_next     = (upcoming[1] if upcoming else "the track").replace(" — SEASON FINALE", "")
    prompt = (
        f"It's Friday, and Race {race_num_next} at {track_next} is coming up this "
        f"Monday, green flag 8PM ET. You're Dale Earnhardt Sr. "
        f"Give an unprompted opinion or observation about something racing related. "
        f"Could be about the QSR season so far, real NASCAR news, oval racing in general, "
        f"a life lesson from racing, or just something on your mind. "
        f"Keep it to 2-4 sentences. Sound natural, like you just walked into the garage "
        f"and said something. No greeting needed — just the take. "
        f"Current mood: {mood}. Race {race_num - 1} completed so far this season."
    )
    response = await ask_claude(prompt, user_context=mood_context())
    if response:
        embed = discord.Embed(
            description=f"💭 {response}",
            color=0xE8272A,
            timestamp=datetime.utcnow()
        )
        embed.set_author(name="Dale's Take")
        embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series 🏁")
        await ch.send(embed=embed)
    mark_fired("weekly_take", now)


# ─────────────────────────────────────────────────────────────────
#  PRE-RACE TRASH TALK
# ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def pre_race_trash_talk():
    """30 minutes before race, Dale calls out a rivalry."""
    now = now_et()
    if not should_fire("trash_talk", 19, 30, now):
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild or not ANTHROPIC_API_KEY:
        return
    ch = discord.utils.get(guild.text_channels, name="series-announcements")
    if not ch:
        return
    data      = load_data()
    standings = compute_adjusted_standings(data)
    if len(standings) < 2:
        return
    sorted_s   = standings_sorted(standings)
    top5_names = [name for name, _ in sorted_s[:5]]
    rivalry_ctx = get_rivalry_context()
    prompt = (
        f"It's 30 minutes before the QSR High Horsepower Series race tonight. "
        f"You're Dale Earnhardt Sr. Look at these top standings: {top5_names}. "
        f"{rivalry_ctx} "
        f"Pick two drivers who are close in points or have a heated rivalry and call it out. "
        f"Make a bold prediction or stir the pot a little. "
        f"2-3 sentences max. Sound like pre-race Dale — confident, a little ornery. "
        f"Start with something like 'I tell you what...' or 'Y'all better watch...' or similar."
    )
    response = await ask_claude(prompt, user_context=mood_context())
    if response:
        embed = discord.Embed(
            title="🏁 Dale's Pre-Race Call",
            description=response,
            color=0xE8272A,
            timestamp=datetime.now(ET)
        )
        embed.set_footer(text="Green flag in 30 minutes | @everyone")
        await ch.send("@everyone", embed=embed)
    mark_fired("trash_talk", now)


# ─────────────────────────────────────────────────────────────────
#  POST-RACE REACTION & RECAP
# ─────────────────────────────────────────────────────────────────

WEAPON_POLL_HOURS = 48   # voting window before auto-close

def _poll_answer_text(name: str, incidents: int) -> str:
    """Discord poll answers cap at 55 characters — truncate defensively."""
    label = f"{name} — {incidents}x"
    return label if len(label) <= 55 else label[:52] + "..."


async def post_weapon_of_week_poll(ch, race_num: int, results: list) -> tuple:
    """Post a 'Weapon of the Week' poll — top 4 by incident count.

    Fully best-effort and isolated from the rest of the recap. A poll
    failure (permissions, a discord.py version without Poll support, a
    Discord outage) must never block Dale's text reaction or the
    standings/team fields from posting — those matter more and are already
    working. Everything here is wrapped so a failure just skips the poll.

    Returns (posted: bool, message: str) so a manual trigger (/weaponpoll)
    can tell the admin exactly what happened rather than the result only
    showing up in Railway's console logs where nobody watching Discord
    would ever see it.
    """
    try:
        ranked = sorted(
            [r for r in results if r.get("incidents", 0) > 0],
            key=lambda r: r.get("incidents", 0), reverse=True)
        if len(ranked) < 2:
            msg = (f"Skipped — fewer than 2 drivers had incidents in Race {race_num} "
                  f"({len(ranked)} qualifying). Nothing to vote on.")
            print(f"Weapon of the Week: {msg}")
            return False, msg
        top4 = ranked[:4]

        poll = discord.Poll(
            question=f"🚨 Race {race_num} Weapon of the Week",
            duration=timedelta(hours=WEAPON_POLL_HOURS),
            multiple=False,
        )
        for r in top4:
            poll.add_answer(text=_poll_answer_text(r["name"], r.get("incidents", 0)))

        msg = await ch.send(content="Who was driving the demolition derby tonight? 👀",
                            poll=poll)

        data = load_data()
        data.setdefault("polls", []).append({
            "id":          f"weapon_{race_num}_{int(datetime.utcnow().timestamp())}",
            "type":        "weapon_of_the_week",
            "race_number": race_num,
            "channel_id":  str(ch.id),
            "message_id":  str(msg.id),
            "question":    f"Race {race_num} Weapon of the Week",
            "options":     [{"name": r["name"], "incidents": r.get("incidents", 0)}
                            for r in top4],
            "created_at":  datetime.utcnow().isoformat(),
            "updated_at":  datetime.utcnow().isoformat(),
            "closes_at":   (datetime.utcnow() + timedelta(hours=WEAPON_POLL_HOURS)).isoformat(),
            "closed":      False,
            "results":     None,
        })
        save_data(data)
        msg = f"Posted for Race {race_num}: {', '.join(a['name'] for a in poll.answers) if hasattr(poll,'answers') else ', '.join(r['name'] for r in top4)}"
        print(f"✅ Weapon of the Week poll posted for Race {race_num}")
        return True, msg
    except Exception as e:
        msg = f"Failed to post: {e}"
        print(f"⚠️ Weapon of the Week poll failed for Race {race_num}: {e}")
        return False, msg


async def post_race_reaction(guild: discord.Guild, race_num: int, results: list, sub_id: str):
    if not ANTHROPIC_API_KEY or not results:
        return
    # Was "race-results" — a channel that doesn't match the app's own
    # CH_RECAP constant ("dales-post-race"), the restructure command's
    # channel list, or the project's own documentation. Dale's reaction was
    # landing somewhere nobody was looking for it.
    ch = discord.utils.get(guild.text_channels, name="dales-post-race")
    if not ch:
        return
    top3      = results[:3]
    incidents = [(r["name"], r.get("incidents", 0)) for r in results if r.get("incidents", 0) >= 10]
    clean     = [r["name"] for r in results if r.get("incidents", 0) == 0]
    results_summary = f"Race {race_num} results: Winner: {top3[0]['name']}. "
    if len(top3) > 1:
        results_summary += f"2nd: {top3[1]['name']}. "
    if len(top3) > 2:
        results_summary += f"3rd: {top3[2]['name']}. "
    if incidents:
        results_summary += f"High incidents: {', '.join(f'{n} ({i}x)' for n, i in incidents)}. "
    if clean:
        results_summary += f"Ran clean: {', '.join(clean[:3])}."
    streak_callouts  = update_streaks(results)
    rivalry_callouts = update_rivalries(results)
    rivalry_ctx      = get_rivalry_context()

    # Team championship. Recalc first so this reflects the race just scored,
    # and so drops/join-date windows are applied rather than stale totals.
    try:
        recalc_team_points()
    except Exception as e:
        print(f"⚠️ team recalc before recap failed: {e}")
    reg_now = load_reg()
    teams_ranked = sorted(
        [t for t in reg_now.get("teams", []) if t.get("members")],
        key=lambda t: t.get("points", 0), reverse=True)

    team_ctx = ""
    if teams_ranked:
        team_ctx = ("Team championship after this race: "
                    + "; ".join(f"{i+1}. {t['name']} {t.get('points',0)}pts"
                                for i, t in enumerate(teams_ranked[:5])) + ". ")

    # Individual championship standings — real numbers from data.json.
    # Previously the prompt gave Dale zero point totals to work with, so
    # when his reaction text mentioned who "sits on top" with X points, he
    # was inventing that figure outright. It never came from data.json, and
    # it never matched !standings. Ground him in the actual top 5 and tell
    # him explicitly not to cite anything outside that list.
    data_now         = load_data()
    adjusted         = compute_adjusted_standings(data_now)
    standings_ranked = standings_sorted(adjusted) if adjusted else []
    standings_ctx    = ""
    standings_field  = ""
    if standings_ranked:
        standings_ctx = ("Championship standings after this race (these are the ONLY "
                          "point totals that exist — if you mention anyone's points, "
                          "use these exact numbers, never a different one, and never "
                          "cite a point total for a driver not listed here): "
                          + "; ".join(f"{i+1}. {name} {info['points']}pts"
                                      for i, (name, info) in enumerate(standings_ranked[:5]))
                          + ". ")
        medals = ["🥇", "🥈", "🥉"]
        standings_field = "\n".join(
            f"{medals[i] if i < 3 else f'**{i+1}.**'} **{name}** — {info['points']} pts"
            for i, (name, info) in enumerate(standings_ranked[:5])
        )

    prompt = (
        f"You just watched Race {race_num} of the QSR High Horsepower Series. "
        f"Here's what happened: {results_summary} "
        f"{rivalry_ctx} "
        f"{team_ctx}"
        f"{standings_ctx}"
        f"Give a post-race reaction in Dale's voice. "
        f"Comment on the winner, maybe someone who impressed or disappointed you, "
        f"and if there were wreckers, give your honest opinion. "
        f"If there's a hot rivalry brewing, call it out. "
        f"If the team championship is close at the top, mention it in one line. "
        f"IMPORTANT: the race winner and the championship points leader are not "
        f"always the same driver — check the standings list above before saying "
        f"anyone 'leads' or 'sits on top' of the championship. Only say that about "
        f"the driver actually listed first in the standings above. "
        f"If you reference anyone's championship points, use only the exact "
        f"numbers given above — never estimate, round, or make one up. "
        f"3-5 sentences. Sound like Dale in victory lane or the garage after a race. "
        f"Current mood: {get_dale_mood()}."
    )
    response = await ask_claude(prompt, user_context=mood_context())
    if response:
        embed = discord.Embed(
            title=f"🏁 Dale's Race {race_num} Reaction",
            description=response,
            color=0xE8272A,
            timestamp=datetime.utcnow()
        )
        if streak_callouts:
            embed.add_field(name="🔥 Streak Alert", value="\n".join(streak_callouts), inline=False)
        if rivalry_callouts:
            embed.add_field(name="⚔️ Rivalry Watch", value="\n".join(rivalry_callouts), inline=False)
        if teams_ranked:
            medals = ["🥇", "🥈", "🥉"]
            lines = []
            for i, t in enumerate(teams_ranked[:6]):
                tag = medals[i] if i < 3 else f"**{i+1}.**"
                roster = len(t.get("members", []))
                lines.append(f"{tag} **{t['name']}** — {t.get('points', 0)} pts "
                             f"({roster} driver{'s' if roster != 1 else ''})")
            gap = ""
            if len(teams_ranked) > 1:
                d = teams_ranked[0].get("points", 0) - teams_ranked[1].get("points", 0)
                gap = f"\n\n*{teams_ranked[0]['name']} leads by {d} pt{'s' if d != 1 else ''}.*"
            embed.add_field(name="🏎️ Team Championship",
                            value="\n".join(lines) + gap, inline=False)
        embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series")
        if standings_field:
            embed.add_field(name="🏆 Championship Standings", value=standings_field, inline=False)
        await ch.send(embed=embed)
    await post_weapon_of_week_poll(ch, race_num, results)
    total_incidents = sum(r.get("incidents", 0) for r in results)
    avg_incidents   = total_incidents / len(results) if results else 0
    if avg_incidents > 10:
        set_dale_mood("grumpy", f"Race {race_num} was a mess — avg {avg_incidents:.1f} incidents")
    elif avg_incidents < 4:
        set_dale_mood("good", f"Race {race_num} was clean racing — avg {avg_incidents:.1f} incidents")
    else:
        set_dale_mood("neutral", f"Race {race_num} was average")


# ─────────────────────────────────────────────────────────────────
#  NEWCOMER CALLOUT
# ─────────────────────────────────────────────────────────────────

async def newcomer_callout(guild: discord.Guild, driver_name: str):
    if not ANTHROPIC_API_KEY:
        return
    ch = discord.utils.get(guild.text_channels, name="series-announcements")
    if not ch:
        return
    prompt = (
        f"A new driver named {driver_name} just registered for their first QSR High Horsepower Series race. "
        f"Welcome them in Dale Earnhardt Sr.'s voice. "
        f"Be welcoming but also let them know this is real racing — "
        f"earn your stripes on track. 2-3 sentences. Genuine but Dale-tough."
    )
    response = await ask_claude(prompt)
    if response:
        embed = discord.Embed(
            title="🏁 New Driver in the Garage",
            description=response,
            color=0xE8272A,
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series")
        await ch.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  DALE'S PREDICTION
# ─────────────────────────────────────────────────────────────────

PREDICTION_FILE = os.path.join(_DATA_DIR, "dale_prediction.json")

@tasks.loop(minutes=1)
async def race_prediction():
    """Dale posts the odds board and a race prediction 1 hour before green flag."""
    now = now_et()
    if not should_fire("prediction", 19, 0, now):
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    ch = discord.utils.get(guild.text_channels, name="series-announcements")
    if not ch:
        return
    data      = load_data()
    standings = compute_adjusted_standings(data)
    schedule  = data.get("schedule", [])
    race_num  = data.get("race_number", 1)
    sorted_s   = standings_sorted(standings)
    top5_names = [name for name, _ in sorted_s[:5]]
    track = ""
    if schedule and len(schedule) >= race_num:
        track = schedule[race_num - 1].get("track", "tonight's track")

    # Part 1 — LOBBY UP ping. This is the call to action, so it goes first
    # and carries the @arca mention; the odds board and prediction ride behind it.
    track_clean = (track or "").replace(" — SEASON FINALE", "").strip() or "the track"
    await ch.send(
        f"🟢 **LOBBY UP — RACE {race_num}: {track_clean.upper()}**\n"
        f"<@&{ARCA_ROLE_ID}>\n\n"
        f"🕖 **7:00 PM ET** — Lobby open / Practice\n"
        f"🕢 **7:50 PM ET** — Qualifying\n"
        f"🕗 **8:00 PM ET** — Green flag\n\n"
        f"Hop in and get your laps. See you out there. 🏁"
    )

    # Part 2 — lock Dale's Book. The board itself now opens on Tuesday (see
    # weekly_settlement below) and stays open all week, so by the time this
    # fires — 1 hour before green flag — the job here is just to close it
    # before anyone can bet with live-race knowledge. We still read the
    # existing board (not rebuild it) to grab the favorite for Dale's
    # prediction below.
    reg   = load_reg()
    board = data.get("odds_board") or {}
    favorite = None
    if board.get("race_number") == race_num and board.get("moneyline"):
        favorite = max(board["moneyline"].items(), key=lambda kv: kv[1]["prob"])[0]
        if board.get("open"):
            board["open"] = False
            save_data(data)
            sb_ch = discord.utils.get(guild.text_channels, name=SPORTSBOOK_CHANNEL)
            if sb_ch:
                await sb_ch.send(
                    f"🔒 **Dale's Book is closed** for Race {race_num}. "
                    f"Good luck out there — settlement posts Tuesday at noon ET."
                )
    else:
        print(f"⚠️ No open Dale's Book board found for Race {race_num} at lock time — "
              f"nothing to close (was it opened Tuesday?).")

    # Part 3 — Dale's narrative prediction, grounded in the actual favorite
    # from the odds engine rather than pure vibes.
    if ANTHROPIC_API_KEY:
        prompt = (
            f"It's one hour before green flag for Race {race_num} at {track} in the "
            f"QSR High Horsepower Series. The lobby is open and drivers are joining now. "
            f"Current top 5 in standings: {top5_names}. "
            f"Dale's Book has {favorite or 'nobody'} as the betting favorite tonight. "
            f"Make a bold race prediction as Dale Earnhardt Sr. — you can agree with the book's "
            f"favorite or call for an upset. Maybe flag a surprise storyline to watch. "
            f"2-3 sentences. Confident. Dale doesn't hedge his bets."
        )
        response = await ask_claude(prompt, user_context=mood_context())
        if response:
            with open(PREDICTION_FILE, "w") as f:
                json.dump({"race_num": race_num, "prediction": response, "correct": None}, f)
            embed = discord.Embed(
                title=f"🔮 Dale's Race {race_num} Prediction",
                description=response,
                color=0xFFD700,
                timestamp=datetime.now(ET)
            )
            embed.set_footer(text="Hold Dale accountable after the race 👀")
            await ch.send(embed=embed)
    mark_fired("prediction", now)


# ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def weekly_settlement():
    """Every Tuesday at noon ET, grade last night's bets against posted
    results and post the recap + updated money leaderboard. If results
    haven't been pushed from Race Control yet, this skips quietly and an
    admin can run /forcesettle once they're in — it does NOT mark itself
    fired in that case, so it keeps checking on later ticks that same day."""
    now = now_et()
    if now.weekday() != 1 or not at_time(now, 12, 0):   # 1 = Tuesday
        return
    if already_fired("settlement", now):
        return
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    data = load_data()
    reg  = load_reg()
    board = data.get("odds_board") or {}
    race_num = board.get("race_number")
    if not race_num:
        mark_fired("settlement", now)
        return
    changes = grade_and_settle_race(data, reg, race_num)
    if not changes:
        return   # results not posted yet — try again next tick today
    save_data(data)

    ch = discord.utils.get(guild.text_channels, name=SPORTSBOOK_CHANNEL)
    if ch:
        winners = sum(1 for c in changes if c[3] == "won")
        bal = data.get("economy", {}).get("balances", {})
        id_to_name = {str(d.get("discord_id")): d.get("name") for d in reg.get("drivers", [])}
        # Only drivers who've actually bet in MIN_RACES_FOR_PRIZE+ races count
        # toward the "leading" board — otherwise someone parked on the
        # untouched starting balance shows up as the leader.
        eligible = [(did, amt) for did, amt in bal.items()
                    if races_bet_on(data, did) >= MIN_RACES_FOR_PRIZE]
        ranked = sorted(eligible, key=lambda kv: kv[1], reverse=True)[:5]
        lead_lines = [f"**{i+1}.** {id_to_name.get(did, f'<@{did}>')} — ${amt}"
                      for i, (did, amt) in enumerate(ranked)]
        embed = discord.Embed(
            title=f"🎰 Dale's Book — Race {race_num} Settled",
            description=f"{winners}/{len(changes)} bets cashed. Here's the money board:",
            color=0x2ECC71,
        )
        embed.add_field(
            name=f"Top 5 (eligible — {MIN_RACES_FOR_PRIZE}+ races bet)",
            value="\n".join(lead_lines) or "Nobody's eligible yet.", inline=False)
        await ch.send(embed=embed)

    # Open next week's board right after settling — Dale's Book runs
    # Tuesday-through-lobby, so this is the start of that window. Only
    # fires if race_number has actually moved past the race we just
    # settled (i.e. Race Control has pushed results); if it hasn't,
    # opening now would just re-open a board for the race that already
    # happened, so we skip and leave it for a manual /postodds once
    # results are in.
    next_race = data.get("race_number", race_num)
    if next_race > race_num:
        new_board, problems = build_odds_board(data, reg)
        if problems:
            print(f"⚠️ Dale's Book validation FAILED opening Race {next_race}, not posting: {problems}")
        else:
            save_data(data)
            if ch:
                await ch.send(
                    f"🟢 **Dale's Book is OPEN** for Race {next_race} — bet all week, "
                    f"closes at lobby-up next race night."
                )
                await ch.send(embed=format_odds_embed(new_board))
    else:
        print(f"ℹ️ Dale's Book not reopened yet — race_number still {race_num} "
              f"(results not pushed from Race Control). Run !postodds once they are.")

    mark_fired("settlement", now)


# ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=30)
async def close_expired_polls():
    """Finalise any poll past its closing time, record final vote counts,
    announce the winner. Runs independently of race night — safe to fire
    any time, and skips cleanly if the bot isn't fully ready yet."""
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    data  = load_data()
    polls = data.get("polls", [])
    if not polls:
        return

    now = datetime.utcnow()
    changed = False
    for p in polls:
        if p.get("closed"):
            continue
        try:
            closes_at = datetime.fromisoformat(p["closes_at"])
        except Exception:
            continue
        if now < closes_at:
            continue
        try:
            ch = guild.get_channel(int(p["channel_id"]))
            if not ch:
                continue
            msg = await ch.fetch_message(int(p["message_id"]))
            if not msg.poll:
                continue
            try:
                await msg.end_poll()
            except Exception:
                pass   # Discord may have already closed it on schedule

            results = [{"name": a.text, "votes": a.vote_count} for a in msg.poll.answers]
            p["results"]    = results
            p["closed"]     = True
            p["updated_at"] = datetime.utcnow().isoformat()
            changed = True

            if results and max(r["votes"] for r in results) > 0:
                winner = max(results, key=lambda r: r["votes"])
                driver_name = winner["name"].split(" — ")[0]
                await ch.send(f"🏆 **Weapon of the Week** goes to... "
                             f"**{driver_name}**! ({winner['votes']} votes) 💀")
        except Exception as e:
            print(f"⚠️ Could not close poll {p.get('id')}: {e}")

    if changed:
        save_data(data)


@bot.event
async def on_ready():
    bot.add_view(RoleSelectView())      # Re-register persistent views on restart
    bot.add_view(RegistrationView())
    bot.add_view(RSVPView())
    print(f"✅  Ask Dale Bot online as {bot.user}")

    # ── Slash command sync ──────────────────────────────────────
    # Every hybrid command below is registered globally the moment its
    # decorator runs. copy_global_to() copies those global definitions
    # into this guild's command bucket, and sync(guild=...) pushes that
    # bucket to Discord — which OVERWRITES whatever was registered for
    # this guild before, including any stale/ghost slash commands left
    # behind by older deploys. That overwrite is what fixes commands
    # like /standings responding with "The application did not respond":
    # the ghost had no live handler behind it; this sync replaces it
    # with the real one.
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild_obj)
        synced = await bot.tree.sync(guild=guild_obj)
        print(f"✅  Synced {len(synced)} slash commands to guild {GUILD_ID}")
    except Exception as e:
        print(f"⚠️  Slash command sync failed: {e}")

    dales_weekly_take.start()
    pre_race_trash_talk.start()
    race_prediction.start()
    weekly_settlement.start()
    race_announcement_scheduler.start()
    close_expired_polls.start()
    await bot.change_presence(activity=discord.Game("QSR High Horsepower Series 🏁"))
    if ANTHROPIC_API_KEY:
        print("✅  Claude AI enabled — Ask Dale is fully intelligent!")
    else:
        print("⚠️  No ANTHROPIC_API_KEY — using FAQ mode only")


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    in_ask_dale_channel = (message.channel.name == ASK_DALE_CH)
    was_mentioned = bot.user in message.mentions
    is_command = message.content.startswith("!")
    if in_ask_dale_channel and is_command:
        await bot.process_commands(message)
        return
    should_respond = in_ask_dale_channel or was_mentioned
    if should_respond:
        question = message.content
        for mention in [f"<@{bot.user.id}>", f"<@!{bot.user.id}>"]:
            question = question.replace(mention, "").strip()
        if not question:
            question = "hey"
        q_lower = question.lower()
        daytona_keywords = ["daytona 2001", "february 18", "february 2001",
                           "how did you die", "crash 2001", "dale died",
                           "earnhardt died", "2001 crash", "that crash"]
        if any(kw in q_lower for kw in daytona_keywords):
            embed = discord.Embed(
                description=(
                    "...I don't much like talkin' about that day. "
                    "Some things you just carry. "
                    "We ain't doin' this. 🏁"
                ),
                color=0x222222
            )
            embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series")
            await message.reply(embed=embed)
            await bot.process_commands(message)
            return
        async with message.channel.typing():
            if ANTHROPIC_API_KEY:
                channel_id = message.channel.id
                discord_history = []
                try:
                    # Only pull recent history. Reading far back is how a
                    # conversation with one member bled into the next member's
                    # question — Dale answered a new person as if they were
                    # mid-argument with him.
                    cutoff = message.created_at - timedelta(minutes=HISTORY_MAX_AGE_MIN)
                    last_human = None
                    async for msg in message.channel.history(limit=15, before=message):
                        if msg.created_at < cutoff:
                            break
                        if msg.author.bot and msg.author == bot.user:
                            msg_content = msg.content
                            if msg.embeds:
                                msg_content = msg.embeds[0].description or msg.content
                            # Record who Dale was answering, so a reply can't be
                            # mistaken for a reply to the current speaker.
                            who = f" (to {last_human})" if last_human else ""
                            discord_history.insert(
                                0, {"role": "assistant", "content": f"[Dale{who}]: {msg_content}"})
                        elif not msg.author.bot:
                            last_human = msg.author.display_name
                            discord_history.insert(0, {
                                "role": "user",
                                "content": f"{msg.author.display_name}: {msg.content}"
                            })
                except Exception as e:
                    print(f"History read error: {e}")
                combined_history = discord_history[-10:] if discord_history else get_history(channel_id)

                # Flag it explicitly when the person speaking now is not the
                # person Dale was last talking to.
                speaker = message.author.display_name
                prev_speaker = None
                for h in reversed(combined_history):
                    if h["role"] == "user" and ":" in h["content"]:
                        prev_speaker = h["content"].split(":", 1)[0].strip()
                        break
                if prev_speaker and prev_speaker != speaker:
                    combined_history.append({
                        "role": "user",
                        "content": (f"[SYSTEM NOTE: {speaker} is a DIFFERENT person from "
                                    f"{prev_speaker}. A new conversation starts now. Nothing "
                                    f"above was said by {speaker} — do not hold them to it, "
                                    f"and do not continue {prev_speaker}'s thread.]")
                    })
                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    ref_content = ref.content
                    if ref.embeds:
                        ref_content = ref.embeds[0].description or ref.content
                    question = f'[Replying to: "{ref_content}"]\n{question}'
                user_ctx = get_user_context(message.author.id, message.author.display_name)
                user_ctx += get_sender_context(message.author)
                # Label the live question the same way history is labelled.
                # Sending it bare was the core bug: Dale saw a run of named
                # messages then an unnamed one, and assumed the previous
                # speaker was still talking.
                response = await ask_claude(f"{speaker}: {question}",
                                            channel_id, combined_history, user_ctx)
                if response:
                    add_to_history(channel_id, "user", question)
                    add_to_history(channel_id, "assistant", response)
                    update_user_memory(message.author.id, message.author.display_name, question, response)
                    embed = discord.Embed(description=response, color=0xE8272A)
                    embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series 🏁")
                    await message.reply(embed=embed)
                    await bot.process_commands(message)
                    return
            responded = False
            for key, answer in FAQ.items():
                if key in q_lower:
                    embed = discord.Embed(description=answer, color=0xE8272A)
                    embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series 🏁")
                    await message.reply(embed=embed)
                    responded = True
                    break
            if not responded:
                import random
                fallbacks = [
                    "You win some, you lose some, you wreck some. What else you got for me? 🏁",
                    "I hear ya. Ask me somethin' specific and I'll give it to you straight.",
                    "Now that's an interesting one. Try me again with a little more detail, son.",
                    "Shoot. I've heard worse questions in the garage. What's on your mind?",
                    "I ain't got a clean answer on that right now. But ask me about racin' — that I know cold.",
                    "You'd have to ask somebody smarter than me on that one. Now if it's about oval racin', different story.",
                    "I tell you what — come race night Monday, THAT'S when Dale's got all the answers. 🏁",
                ]
                await message.reply(random.choice(fallbacks))
    await bot.process_commands(message)


@bot.event
async def on_member_join(member: discord.Member):
    ch = discord.utils.get(member.guild.text_channels, name="welcome")
    if ch:
        embed = discord.Embed(
            title="🏁  Welcome to QSR Simulations!",
            description=(
                f"Well, look who just pulled into the garage — {member.mention}! Welcome to QSR Simulations.\n\n"
                "**QSR High Horsepower Series** — We run the ARCA car at full 110% horsepower. "
                "No restrictions. Real power. Real racin'.\n\n"
                "**Here's what you need to do:**\n"
                "1️⃣  Grab your roles → `#get-roles`\n"
                "2️⃣  Read the rules → `#league-rules`\n"
                "3️⃣  Register & pick your car number → `#registration`\n\n"
                "Any questions, you ask Dale in `#ask-dale`. "
                "I'll tell you straight. See you on the track, son. 🏁"
            ),
            color=0xE8272A
        )
        embed.set_footer(text="QSR Simulations | High Horsepower Series")
        await ch.send(embed=embed)



# ─────────────────────────────────────────────────────────────────
#  MEMBER COMMANDS
# ─────────────────────────────────────────────────────────────────

@bot.hybrid_command(name="ask", description="Ask Dale anything — rules, iRacing, NASCAR, racing tips")
@has_arca()
async def ask(ctx, *, question: str = ""):
    if not question:
        await ctx.send(
            "Well shoot, you gotta ask me somethin' son. Try:\n"
            "`!ask how do stage points work`\n"
            "`!ask what is bump drafting`\n"
            "`!ask how do I protest someone`\n"
            "`!ask what is iRating`\n"
            "`!ask tips for drafting on ovals`"
        )
        return
    async with ctx.typing():
        q_lower = question.lower()
        daytona_keywords = ["daytona 2001", "february 18", "february 2001", "how did you die",
                           "crash 2001", "dale died", "earnhardt died", "the crash"]
        if any(kw in q_lower for kw in daytona_keywords):
            embed = discord.Embed(
                description=(
                    "...I don't much like talkin' about that day. Some things you just carry with you. "
                    "Let's talk about somethin' else. 🏁"
                ),
                color=0x333333
            )
            embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series")
            await ctx.send(embed=embed)
            return
        if ANTHROPIC_API_KEY:
            user_ctx = get_user_context(ctx.author.id, ctx.author.display_name)
            user_ctx += get_sender_context(ctx.author)
            response = await ask_claude(question, user_context=user_ctx)
            if response:
                embed = discord.Embed(description=response, color=0xE8272A)
                embed.set_footer(text="Ask Dale #3 | QSR High Horsepower Series 🏁")
                await ctx.send(embed=embed)
                return
        q = question.lower()
        for key, answer in FAQ.items():
            if key in q:
                embed = discord.Embed(description=answer, color=0xE8272A)
                embed.set_footer(text="QSR High Horsepower | Ask Dale")
                await ctx.send(embed=embed)
                return
        await ctx.send(
            "I'll be honest with ya, I ain't got a good answer for that one. "
            "Head on over to `#help-desk` or tag an @Admin and they'll sort you out. "
            "Ask me somethin' about racin' though — that I can handle. 🏁"
        )

@bot.hybrid_command(name="dale", description="Ask Dale anything (same as /ask)")
@has_arca()
async def dale(ctx, *, question: str = ""):
    await ask(ctx, question=question)

@bot.hybrid_command(name="standings", description="Current championship standings")
@has_arca()
async def standings(ctx):
    data = load_data()
    s = compute_adjusted_standings(data)
    if not s:
        await ctx.send("No standings yet — Race 1 incoming! 🏁")
        return
    sorted_s = standings_sorted(s)
    embed    = discord.Embed(
        title="🏆 QSR High Horsepower Series — Championship Standings",
        color=0xE8272A,
        timestamp=datetime.utcnow()
    )
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines  = []
    for i, (driver, info) in enumerate(sorted_s[:40], 1):
        icon    = medals.get(i, f"`{i:>2}.`")
        wins    = info.get("wins", 0)
        win_str = f" ⭐x{wins}" if wins else ""
        # Drop-adjusted points alone read as "missing data" the moment drops
        # kick in (a driver's total can look like it lost 40+ points
        # overnight). Show the raw cumulative total and the counted/started
        # fraction right on the line — same shape as SRH's own RACES
        # COUNTED / STARTS columns — so nobody has to go hunting for why.
        raw     = info.get("raw_points", info["points"])
        counted = info.get("counted", 0)
        starts  = counted + len(info.get("dropped", []) or [])
        detail  = f" · {raw} raw · {counted}/{starts} counted" if starts else ""
        lines.append(f"{icon} **{driver}** — {info['points']} pts{detail}{win_str}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Through Race {data.get('race_number',1)-1} | Each driver's {SEASON_DROPS} worst races are dropped | Updated after each race by Race Control Bot")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="schedule", description="Season race schedule")
@has_arca()
async def schedule_cmd(ctx):
    data  = load_data()
    sched = data.get("schedule", [])
    if not sched:
        await ctx.send("📅 Schedule not loaded yet. Check back soon!")
        return
    embed = discord.Embed(title="📅 QSR High Horsepower — Season Schedule", color=0xE8272A)
    lines = []
    for i, race in enumerate(sched, 1):
        done = "✅" if race.get("complete") else "🔜"
        lines.append(f"{done} **Race {i}** — {race['track']} | {race['date']}")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

@bot.hybrid_command(name="rules", description="Quick rules summary")
@has_arca()
async def rules_cmd(ctx):
    embed = discord.Embed(title="📋 QSR High Horsepower Series — Quick Rules", color=0xE8272A)
    embed.add_field(name="Car",                  value="ARCA Menards @ 110% HP", inline=True)
    embed.add_field(name="Race Day",             value="Mondays 8PM ET", inline=True)
    embed.add_field(name="Points",               value="2026 NASCAR system (55 pts win, +1 fastest lap) · 2 worst results dropped", inline=True)
    embed.add_field(name="Stages",               value="1 stage per race, green flag — no caution", inline=True)
    embed.add_field(name="Incident Limit",       value="17x per race", inline=True)
    embed.add_field(name="Intentional Wrecking", value="Immediate DQ", inline=True)
    embed.add_field(name="Appeals",              value="$1 deposit, refunded if upheld", inline=True)
    embed.add_field(name="Full Rulebook",        value="See `#league-rules`", inline=True)
    embed.set_footer(text="Use !ask <question> for more detail on anything")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────



# ─────────────────────────────────────────────────────────────────
#  REGISTRATION — Driver & Team modals + persistent view
# ─────────────────────────────────────────────────────────────────

STAFF_CH = "staff-chat"

class DriverRegModal(discord.ui.Modal, title="🏁 QSR Driver Registration"):
    full_name = discord.ui.TextInput(
        label="Full Name",
        placeholder="e.g. John Smith",
        max_length=50,
        required=True,
    )
    iracing_id = discord.ui.TextInput(
        label="iRacing Customer ID",
        placeholder="Numbers only — find it on iRacing.com",
        max_length=20,
        required=True,
    )
    rules_ack = discord.ui.TextInput(
        label="Rules Acknowledgment",
        placeholder='Type YES to confirm you have read the QSR rulebook',
        max_length=10,
        required=True,
    )

    def __init__(self, chosen_number: str):
        super().__init__()
        # Number is pre-selected via the picker, so it's never free-typed here.
        self.chosen_number = norm_num(chosen_number)

    async def on_submit(self, interaction: discord.Interaction):
        name    = self.full_name.value.strip()
        iid     = self.iracing_id.value.strip()
        ack     = self.rules_ack.value.strip().upper()
        num_final = self.chosen_number
        discord_id = str(interaction.user.id)

        # Rules ack check
        if ack != "YES":
            await interaction.response.send_message(
                "❌ You must type **YES** to acknowledge the rulebook. Try again.",
                ephemeral=True)
            return

        reg = load_reg()

        # Already registered?
        existing = get_driver_reg(discord_id)
        if existing:
            await interaction.response.send_message(
                f"⚠️ You're already registered as **{existing['name']}** "
                f"(#{existing['number']}, Status: {existing['status']}).\n"
                f"Contact an admin to make changes.",
                ephemeral=True)
            return

        # Race-condition guard: number may have been claimed between the
        # picker and this submit. Numbers can't be picked if taken, but two
        # people can grab the last one seconds apart.
        if num_final in taken_numbers():
            await interaction.response.send_message(
                f"❌ **#{num_final}** was just claimed by another driver.\n"
                f"Click **Register as Driver** again and pick a different number.",
                ephemeral=True)
            return

        # Field full?
        conf = confirmed_count()
        status = "Confirmed" if conf < reg["max_field"] else "Waitlist"

        reg["drivers"].append({
            "name":          name,
            "discord_id":    discord_id,
            "discord_tag":   str(interaction.user),
            "iracing_id":    iid,
            "number":        num_final,
            "status":        status,
            "paid":          False,
            "team":          None,
            "registered_at": datetime.utcnow().isoformat(),
        })
        save_reg(reg)

        # Confirm to driver
        if status == "Confirmed":
            msg = (f"✅ **You're in, {name}!**\n"
                   f"Car **#{num_final}** is yours.\n"
                   f"Status: **Confirmed** — pending payment verification.\n"
                   f"Entry fee: **${reg['entry_fee']}** — payment details in `#registration`.\n"
                   f"🏁 Season opens **{SCHEDULE[0]['date']}** at **{SCHEDULE[0]['track']}**. See you there.")
        else:
            pos = sum(1 for d in reg["drivers"] if d["status"] == "Waitlist")
            msg = (f"📋 **{name}, you're on the waitlist** (position {pos}).\n"
                   f"Car **#{num_final}** is reserved for you if a spot opens.\n"
                   f"We'll notify you if you move up. 🏁")

        await interaction.response.send_message(msg, ephemeral=True)

        # Staff notification
        guild    = interaction.guild
        staff_ch = discord.utils.get(guild.text_channels, name=STAFF_CH)
        if staff_ch:
            embed = discord.Embed(
                title="🆕 New Driver Registration",
                color=0x2ecc71 if status == "Confirmed" else 0xf1c40f,
            )
            embed.add_field(name="Name",       value=name,                     inline=True)
            embed.add_field(name="Discord",    value=str(interaction.user),    inline=True)
            embed.add_field(name="Number",     value=f"#{num_final}",          inline=True)
            embed.add_field(name="iRacing ID", value=iid,                      inline=True)
            embed.add_field(name="Status",     value=status,                   inline=True)
            embed.add_field(name="Payment",    value="⏳ Pending",             inline=True)
            embed.set_footer(text=f"Field: {conf+1 if status=='Confirmed' else conf}/{reg['max_field']} confirmed")
            await staff_ch.send(embed=embed)


class TeamRegModal(discord.ui.Modal, title="🏎️ QSR Team Registration"):
    """Create a team. Members are added afterwards via /teaminvite.

    The old version had three free-text 'Driver N Discord Tag' slots that
    were matched with a substring search. That could silently attach the
    wrong driver, and any unmatched text became a ghost member with no
    discord_id — invisible to points and to sync. Nobody consented to being
    added either. Invites replace all of that.
    """
    team_name = discord.ui.TextInput(
        label="Team Name",
        placeholder="e.g. Thunder Racing",
        max_length=50,
        required=True,
    )
    looking = discord.ui.TextInput(
        label="Looking for drivers? (YES / NO)",
        placeholder="YES or NO",
        max_length=3,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        tname     = self.team_name.value.strip()
        looking   = self.looking.value.strip().upper() == "YES"
        owner_id  = str(interaction.user.id)
        owner_tag = str(interaction.user)

        reg = load_reg()

        if get_team(tname):
            await interaction.response.send_message(
                f"❌ A team named **{tname}** already exists. Choose a different name.",
                ephemeral=True)
            return

        owner_reg = get_driver_reg(owner_id)
        if not owner_reg:
            await interaction.response.send_message(
                "❌ You need to register as a driver before creating a team. "
                "Use the **Register as Driver** button in `#registration`.",
                ephemeral=True)
            return

        if owner_reg.get("team"):
            await interaction.response.send_message(
                f"❌ You're already on **{owner_reg['team']}**. Use `/teamleave` first.",
                ephemeral=True)
            return

        join_race = load_data().get("race_number", 1)
        members = [{
            "driver_name": owner_reg["name"],
            "discord_id":  owner_id,
            "discord_tag": owner_tag,
            "joined_race": join_race,
        }]

        team = {
            "name":       tname,
            "owner_id":   owner_id,
            "owner_tag":  owner_tag,
            "members":    members,
            "points":     0,
            "looking":    looking,
            "created_at": datetime.utcnow().isoformat(),
        }
        reg["teams"].append(team)

        # Write the team back onto the driver record. The old code never did
        # this, so owners showed as team-less everywhere and could join a
        # second team because the "already on a team" guard never fired.
        for d in reg["drivers"]:
            if d.get("discord_id") == owner_id:
                d["team"] = tname
        save_reg(reg)

        guild = interaction.guild
        try:
            role = await guild.create_role(name=f"Team: {tname}", mentionable=True)
            await interaction.user.add_roles(role)
            reg = load_reg()
            t = get_team_in(reg, tname)
            if t is not None:
                t["discord_role_id"] = str(role.id)
                save_reg(reg)
        except Exception:
            pass

        # Private team garage — created after the role so the overwrite can
        # reference it.
        vc = None
        try:
            reg = load_reg()
            t   = get_team_in(reg, tname)
            if t is not None:
                vc = await ensure_team_voice(guild, t)
                if vc:
                    t["voice_channel_id"] = str(vc.id)
                    save_reg(reg)
        except Exception:
            pass

        await interaction.response.send_message(
            f"✅ **{tname}** is officially registered!\n"
            f"Owner: {interaction.user.mention}\n"
            f"Roster: 1/4 — invite drivers with `/teaminvite @driver`\n"
            + (f"🔊 Your team garage: {vc.mention}\n" if vc else "") +
            f"{'🔍 Posted in #team-forming — looking for drivers!' if looking else ''}\n"
            f"Points accumulate from Race {join_race} forward. 🏁",
            ephemeral=True)

        if looking:
            tf_ch = discord.utils.get(guild.text_channels, name="team-forming")
            if tf_ch:
                embed = discord.Embed(
                    title=f"🏎️ {tname} — Looking for Drivers!",
                    description=(
                        f"**Owner:** {interaction.user.mention}\n"
                        f"**Roster:** {owner_reg['name']}\n"
                        f"**Spots Available:** 3\n\n"
                        f"DM {interaction.user.mention}, or ask them to "
                        f"`/teaminvite` you. You can also use `/jointeam {tname}`."
                    ),
                    color=0xE8272A,
                )
                embed.set_footer(text="QSR High Horsepower Series — Team Registration")
                await tf_ch.send(embed=embed)

        staff_ch = discord.utils.get(guild.text_channels, name=STAFF_CH)
        if staff_ch:
            await staff_ch.send(
                f"🏎️ New team registered: **{tname}** | Owner: {interaction.user.mention}")


# ── Car-number picker (Option A) ─────────────────────────────────
# A short-lived, per-user ephemeral flow that only ever offers numbers
# that are still available, so a taken number can't be selected at all.

class _NumberSelect(discord.ui.Select):
    """Step 2: pick a specific open number from a <=25 chunk."""
    def __init__(self, numbers: list):
        options = [discord.SelectOption(label=f"#{n}", value=n)
                   for n in numbers[:25]]
        super().__init__(placeholder="Pick your car number…",
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "NumberPickerView" = self.view
        view.chosen_number = self.values[0]
        view.clear_items()
        view.add_item(_ContinueButton(view.chosen_number))
        await interaction.response.edit_message(
            content=f"You picked **#{view.chosen_number}** ✅\n"
                    f"Click below to finish your registration.",
            view=view)


class _RangeSelect(discord.ui.Select):
    """Step 1: pick a range (only used when >25 numbers are open)."""
    def __init__(self, chunks: list):
        self.chunks = chunks
        options = [
            discord.SelectOption(
                label=f"#{ch[0]} – #{ch[-1]}",
                description=f"{len(ch)} open",
                value=str(i),
            )
            for i, ch in enumerate(chunks)
        ]
        super().__init__(placeholder="Step 1 — pick a number range…",
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "NumberPickerView" = self.view
        chunk = self.chunks[int(self.values[0])]
        view.clear_items()
        view.add_item(_NumberSelect(chunk))
        await interaction.response.edit_message(
            content="**Step 2** — pick your car number:", view=view)


class _ContinueButton(discord.ui.Button):
    """Opens the registration modal with the chosen number locked in."""
    def __init__(self, number: str):
        super().__init__(label=f"Continue with #{number}",
                         style=discord.ButtonStyle.success, emoji="🏁")
        self.number = number

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DriverRegModal(self.number))


class NumberPickerView(discord.ui.View):
    """Ephemeral, per-user. Chunks available numbers into <=25 groups so the
    second dropdown is always valid. If <=25 numbers are open, skips straight
    to the number select."""
    def __init__(self):
        super().__init__(timeout=300)
        self.chosen_number = None
        avail = available_numbers()
        chunks = [avail[i:i + 25] for i in range(0, len(avail), 25)]
        if len(chunks) <= 1:
            self.add_item(_NumberSelect(avail))
        else:
            self.add_item(_RangeSelect(chunks))


# ── Car-number CHANGE picker ─────────────────────────────────────
# Same two-step ephemeral flow as registration, but for an already-
# registered driver swapping to a different open number. Confirms
# directly instead of opening the registration modal, then notifies
# staff-chat of the change.

class _ChangeNumberSelect(discord.ui.Select):
    """Step 2: pick a specific open number from a <=25 chunk."""
    def __init__(self, numbers: list):
        options = [discord.SelectOption(label=f"#{n}", value=n)
                   for n in numbers[:25]]
        super().__init__(placeholder="Pick your new car number…",
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "ChangeNumberView" = self.view
        view.chosen_number = self.values[0]
        view.clear_items()
        view.add_item(_ChangeConfirmButton(view.chosen_number))
        await interaction.response.edit_message(
            content=f"You picked **#{view.chosen_number}** ✅\n"
                    f"Click below to confirm the change.",
            view=view)


class _ChangeRangeSelect(discord.ui.Select):
    """Step 1: pick a range (only used when >25 numbers are open)."""
    def __init__(self, chunks: list):
        self.chunks = chunks
        options = [
            discord.SelectOption(
                label=f"#{ch[0]} – #{ch[-1]}",
                description=f"{len(ch)} open",
                value=str(i),
            )
            for i, ch in enumerate(chunks)
        ]
        super().__init__(placeholder="Step 1 — pick a number range…",
                         min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: "ChangeNumberView" = self.view
        chunk = self.chunks[int(self.values[0])]
        view.clear_items()
        view.add_item(_ChangeNumberSelect(chunk))
        await interaction.response.edit_message(
            content="**Step 2** — pick your new car number:", view=view)


class _ChangeConfirmButton(discord.ui.Button):
    """Finalizes the number change and notifies staff-chat."""
    def __init__(self, number: str):
        super().__init__(label=f"Confirm #{number}",
                         style=discord.ButtonStyle.success, emoji="🔁")
        self.number = number

    async def callback(self, interaction: discord.Interaction):
        discord_id = str(interaction.user.id)
        new_final  = norm_num(self.number)

        reg = load_reg()
        driver = next((d for d in reg["drivers"]
                       if d.get("discord_id") == discord_id), None)
        if not driver:
            await interaction.response.edit_message(
                content="❌ You're not registered — contact an admin.",
                view=None)
            return

        old_number = driver.get("number")

        # Race-condition guard: someone else may have grabbed it between
        # the picker opening and this confirm.
        still_taken = any(
            norm_num(d.get("number")) == new_final
            and d.get("discord_id") != discord_id
            and d.get("status") != "Withdrawn"
            for d in reg["drivers"]
        )
        if still_taken:
            await interaction.response.edit_message(
                content=f"❌ **#{new_final}** was just claimed by another driver. "
                        f"Run `/mynumber` again and pick a different one.",
                view=None)
            return

        if new_final == norm_num(old_number):
            await interaction.response.edit_message(
                content=f"You're already **#{new_final}** — no change made.",
                view=None)
            return

        driver["number"] = new_final
        save_reg(reg)

        await interaction.response.edit_message(
            content=f"✅ **Number changed!** You're now car **#{new_final}** (was #{old_number}).",
            view=None)

        # Staff notification
        guild    = interaction.guild
        staff_ch = discord.utils.get(guild.text_channels, name=STAFF_CH)
        if staff_ch:
            embed = discord.Embed(
                title="🔁 Car Number Changed",
                color=0xF1C40F,
            )
            embed.add_field(name="Driver",  value=driver.get("name", "—"), inline=True)
            embed.add_field(name="Discord", value=str(interaction.user),   inline=True)
            embed.add_field(name="Old #",   value=f"#{old_number}",        inline=True)
            embed.add_field(name="New #",   value=f"#{new_final}",         inline=True)
            embed.set_footer(text="QSR High Horsepower Series — Registration Update")
            await staff_ch.send(embed=embed)


class ChangeNumberView(discord.ui.View):
    """Ephemeral, per-user. Same available-numbers chunking as the
    registration picker so a taken number can never be selected."""
    def __init__(self):
        super().__init__(timeout=300)
        self.chosen_number = None
        avail = available_numbers()
        chunks = [avail[i:i + 25] for i in range(0, len(avail), 25)]
        if len(chunks) <= 1:
            self.add_item(_ChangeNumberSelect(avail))
        else:
            self.add_item(_ChangeRangeSelect(chunks))


class RegistrationView(discord.ui.View):
    """Persistent registration embed — two buttons: Driver + Team."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Register as Driver",
        style=discord.ButtonStyle.danger,
        custom_id="reg_driver",
        emoji="🏁",
        row=0,
    )
    async def driver_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Already registered? Stop before they bother picking a number.
        existing = get_driver_reg(str(interaction.user.id))
        if existing:
            await interaction.response.send_message(
                f"⚠️ You're already registered as **{existing['name']}** "
                f"(#{existing['number']}, Status: {existing['status']}).\n"
                f"Contact an admin to make changes.",
                ephemeral=True)
            return

        # Any numbers left?
        if not available_numbers():
            await interaction.response.send_message(
                "❌ Every car number is currently taken. Contact an admin.",
                ephemeral=True)
            return

        await interaction.response.send_message(
            "🏁 **Let's get you registered.**\n"
            "First, choose your car number — only numbers still available are shown.",
            view=NumberPickerView(), ephemeral=True)

    @discord.ui.button(
        label="Register a Team",
        style=discord.ButtonStyle.secondary,
        custom_id="reg_team",
        emoji="🏎️",
        row=0,
    )
    async def team_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TeamRegModal())


@bot.hybrid_command(name="setupregistration", description="Post the registration embed in #registration (admin)")
@is_admin()
async def setup_registration_cmd(ctx):
    """Post persistent registration embed in #registration. Run once."""
    guild = ctx.guild
    ch    = discord.utils.get(guild.text_channels, name="registration")
    if not ch:
        await ctx.send("❌ #registration channel not found. Create it first.")
        return
    reg = load_reg()
    embed = discord.Embed(
        title="🏁 QSR High Horsepower Series — Season 1 Registration",
        description=(
            "**Welcome to the QSR High Horsepower Series.**\n\n"
            "Click **Register as Driver** to claim your spot and car number.\n"
            "Click **Register a Team** to create a team and start earning team points.\n\n"
            f"💰 **Entry Fee:** ${reg['entry_fee']} per driver\n"
            f"🏎️ **Max Field:** {reg['max_field']} drivers\n"
            f"📅 **Season Opener:** {SCHEDULE[0]['date']} — {SCHEDULE[0]['track']}\n"
            f"🏁 **Finale:** {SCHEDULE[-1]['date']} — {SCHEDULE[-1]['track']}\n"
            f"🗓️ **{len(SCHEDULE)} races**, every Monday 8PM ET\n\n"
            "Read the rulebook in `#league-rules` before registering.\n"
            "Questions? Ask Dale in `#ask-dale`. 🏁"
        ),
        color=0xC0392B,
    )
    embed.set_footer(text="QSR Simulations | High Horsepower Series Season 1")
    await ch.send(embed=embed, view=RegistrationView())
    await ctx.send("✅ Registration embed posted in #registration!")


# ─────────────────────────────────────────────────────────────────
#  TEAM MANAGEMENT — create, invite, leave, kick, disband
#  Invite-based by design: the owner invites, the driver accepts. That
#  guarantees a real discord_id on every member and means nobody lands on
#  a roster without agreeing to it.
# ─────────────────────────────────────────────────────────────────

class TeamInviteView(discord.ui.View):
    """Accept/Decline buttons on a team invite. 24h expiry.

    Every guard is re-checked at accept time, not just at send time — the
    team can fill up, the invitee can join elsewhere, or the team can be
    disbanded while the invite sits there.
    """
    def __init__(self, team_name: str, invitee_id: str, inviter_tag: str):
        super().__init__(timeout=86400)
        self.team_name   = team_name
        self.invitee_id  = str(invitee_id)
        self.inviter_tag = inviter_tag

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) != self.invitee_id:
            await interaction.response.send_message(
                "This invite isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        reg    = load_reg()
        team   = get_team_in(reg, self.team_name)
        driver = next((d for d in reg["drivers"]
                       if str(d.get("discord_id")) == self.invitee_id), None)

        if not team:
            await interaction.response.edit_message(
                content=f"❌ **{self.team_name}** no longer exists.", view=None)
            return
        if not driver:
            await interaction.response.edit_message(
                content="❌ You're not registered as a driver. Register in `#registration` first.",
                view=None)
            return
        if get_team_of(reg, self.invitee_id):
            await interaction.response.edit_message(
                content=f"❌ You're already on **{driver.get('team')}**. "
                        f"Use `/teamleave` before joining another team.", view=None)
            return
        if team_is_full(team):
            await interaction.response.edit_message(
                content=f"❌ **{team['name']}** is full ({TEAM_MAX_MEMBERS}/{TEAM_MAX_MEMBERS}).",
                view=None)
            return

        join_race = load_data().get("race_number", 1)
        team["members"].append({
            "driver_name": driver["name"],
            "discord_id":  self.invitee_id,
            "discord_tag": str(interaction.user),
            "joined_race": join_race,
        })
        set_driver_team(reg, self.invitee_id, team["name"])
        save_reg(reg)
        recalc_team_points()

        await apply_team_role(interaction.guild, interaction.user, team, add=True)

        await interaction.response.edit_message(
            content=f"✅ You've joined **{team['name']}**!\n"
                    f"Roster: {len(team['members'])}/{TEAM_MAX_MEMBERS}\n"
                    f"Your team points count from Race {join_race} forward — "
                    f"points you scored before that stay yours individually. 🏁",
            view=None)

        staff_ch = discord.utils.get(interaction.guild.text_channels, name=STAFF_CH)
        if staff_ch:
            await staff_ch.send(
                f"🤝 **{driver['name']}** joined team **{team['name']}** "
                f"(invited by {self.inviter_tag}) — {len(team['members'])}/{TEAM_MAX_MEMBERS}")

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.secondary, emoji="✖️")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"You declined the invite to **{self.team_name}**.", view=None)


class ConfirmDisbandView(discord.ui.View):
    """Two-step confirm for disbanding — it wipes team points irreversibly."""
    def __init__(self, team_name: str, owner_id: str):
        super().__init__(timeout=120)
        self.team_name = team_name
        self.owner_id  = str(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return str(interaction.user.id) == self.owner_id

    @discord.ui.button(label="Disband Team", style=discord.ButtonStyle.danger, emoji="💥")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        reg  = load_reg()
        team = get_team_in(reg, self.team_name)
        if not team:
            await interaction.response.edit_message(
                content="❌ That team no longer exists.", view=None)
            return

        guild = interaction.guild
        for m in team.get("members", []):
            set_driver_team(reg, m.get("discord_id", ""), None)
            member = guild.get_member(int(m["discord_id"])) if str(m.get("discord_id")).isdigit() else None
            await apply_team_role(guild, member, team, add=False)

        rid = team.get("discord_role_id")
        await delete_team_voice(guild, team)
        reg["teams"] = [t for t in reg["teams"]
                        if t["name"].lower() != self.team_name.lower()]
        save_reg(reg)

        if rid:
            try:
                role = guild.get_role(int(rid))
                if role:
                    await role.delete(reason="QSR team disbanded")
            except Exception:
                pass

        await interaction.response.edit_message(
            content=f"💥 **{self.team_name}** has been disbanded. "
                    f"All members are now team-less and team points are gone.",
            view=None)

        staff_ch = discord.utils.get(guild.text_channels, name=STAFF_CH)
        if staff_ch:
            await staff_ch.send(f"💥 Team **{self.team_name}** disbanded by {interaction.user}")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Disband cancelled.", view=None)


@bot.hybrid_command(name="createteam", description="Create a new team")
@has_arca()
async def create_team_cmd(ctx):
    """Open the team creation form. Available any time after signing up."""
    driver = get_driver_reg(str(ctx.author.id))
    if not driver:
        await ctx.send("❌ Register as a driver first in `#registration`.", ephemeral=True)
        return
    if driver.get("team"):
        await ctx.send(f"❌ You're already on **{driver['team']}**. Use `/teamleave` first.",
                       ephemeral=True)
        return
    if ctx.interaction:
        await ctx.interaction.response.send_modal(TeamRegModal())
    else:
        await ctx.send("Use the slash command `/createteam` (the form only opens from slash "
                       "commands), or the **Register a Team** button in `#registration`.")


@bot.hybrid_command(name="teaminvite", description="Invite a driver to your team")
@has_arca()
async def team_invite_cmd(ctx, driver: discord.Member):
    """Owner-only. Sends the driver an invite they accept or decline."""
    reg  = load_reg()
    team = get_team_of(reg, str(ctx.author.id))
    if not team:
        await ctx.send("❌ You're not on a team. Use `/createteam` to start one.",
                       ephemeral=True)
        return
    if str(team.get("owner_id")) != str(ctx.author.id):
        await ctx.send(f"❌ Only the owner of **{team['name']}** can invite drivers.",
                       ephemeral=True)
        return
    if team_is_full(team):
        await ctx.send(f"❌ **{team['name']}** is full ({TEAM_MAX_MEMBERS}/{TEAM_MAX_MEMBERS}).",
                       ephemeral=True)
        return
    if driver.id == ctx.author.id:
        await ctx.send("You're already on your own team. 🙂", ephemeral=True)
        return
    if driver.bot:
        await ctx.send("❌ You can't invite a bot.", ephemeral=True)
        return

    invitee = next((d for d in reg["drivers"]
                    if str(d.get("discord_id")) == str(driver.id)), None)
    if not invitee:
        await ctx.send(f"❌ {driver.mention} isn't registered as a driver yet. "
                       f"They need to register in `#registration` first.", ephemeral=True)
        return
    if get_team_of(reg, str(driver.id)):
        await ctx.send(f"❌ {driver.mention} is already on **{invitee.get('team')}**.",
                       ephemeral=True)
        return

    embed = discord.Embed(
        title="🏎️ Team Invite",
        description=(f"{driver.mention}, **{ctx.author.display_name}** has invited you "
                     f"to join **{team['name']}**.\n\n"
                     f"**Roster:** {len(team['members'])}/{TEAM_MAX_MEMBERS}\n"
                     f"**Current points:** {team.get('points', 0)}\n\n"
                     f"Team points count from the race you join forward. "
                     f"Points you've already scored stay yours individually."),
        color=0xE8272A,
    )
    embed.set_footer(text="Invite expires in 24 hours")
    await ctx.send(content=driver.mention, embed=embed,
                   view=TeamInviteView(team["name"], str(driver.id), str(ctx.author)))


@bot.hybrid_command(name="myteam", description="Your team roster and details")
@has_arca()
async def my_team_cmd(ctx):
    reg  = load_reg()
    team = get_team_of(reg, str(ctx.author.id))
    if not team:
        await ctx.send("You're not on a team yet. Use `/createteam` to start one, "
                       "or `/teams` to see who's recruiting.", ephemeral=True)
        return

    lines = []
    for m in team.get("members", []):
        crown = " 👑" if str(m.get("discord_id")) == str(team.get("owner_id")) else ""
        lines.append(f"• **{m['driver_name']}**{crown} — joined Race {m.get('joined_race', 1)}")

    embed = discord.Embed(title=f"🏎️ {team['name']}", color=0xE8272A)
    embed.add_field(name="Points", value=str(team.get("points", 0)), inline=True)
    embed.add_field(name="Roster",
                    value=f"{len(team.get('members', []))}/{TEAM_MAX_MEMBERS}", inline=True)
    embed.add_field(name="Recruiting",
                    value="Yes 🔍" if team.get("looking") else "No", inline=True)
    embed.add_field(name="Drivers", value="\n".join(lines) or "—", inline=False)
    if str(team.get("owner_id")) == str(ctx.author.id):
        embed.add_field(
            name="Owner Commands",
            value="`/teaminvite @driver` · `/teamkick @driver` · `/teamdisband`",
            inline=False)
    embed.set_footer(text="QSR High Horsepower Series")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="teamleave", description="Leave your current team")
@has_arca()
async def team_leave_cmd(ctx):
    """Leave your team. If the owner leaves, ownership passes to the
    longest-tenured remaining member rather than dissolving the team."""
    reg  = load_reg()
    team = get_team_of(reg, str(ctx.author.id))
    if not team:
        await ctx.send("You're not on a team.", ephemeral=True)
        return

    was_owner = str(team.get("owner_id")) == str(ctx.author.id)
    team["members"] = [m for m in team.get("members", [])
                       if str(m.get("discord_id")) != str(ctx.author.id)]
    set_driver_team(reg, str(ctx.author.id), None)

    note = ""
    if not team["members"]:
        rid = team.get("discord_role_id")
        await delete_team_voice(ctx.guild, team)
        reg["teams"] = [t for t in reg["teams"] if t["name"] != team["name"]]
        note = f"\n**{team['name']}** had no members left and has been dissolved."
        save_reg(reg)
        if rid:
            try:
                role = ctx.guild.get_role(int(rid))
                if role:
                    await role.delete(reason="QSR team empty")
            except Exception:
                pass
    else:
        if was_owner:
            new_owner = promote_next_owner(team)
            note = (f"\nOwnership of **{team['name']}** passed to "
                    f"**{new_owner['driver_name']}** (longest-tenured member).")
        save_reg(reg)
        recalc_team_points()
        await apply_team_role(ctx.guild, ctx.author, team, add=False)

    await ctx.send(f"✅ You've left **{team['name']}**.{note}")

    staff_ch = discord.utils.get(ctx.guild.text_channels, name=STAFF_CH)
    if staff_ch:
        await staff_ch.send(f"👋 **{ctx.author}** left team **{team['name']}**.{note}")


@bot.hybrid_command(name="teamkick", description="Remove a driver from your team (owner only)")
@has_arca()
async def team_kick_cmd(ctx, driver: discord.Member):
    reg  = load_reg()
    team = get_team_of(reg, str(ctx.author.id))
    if not team:
        await ctx.send("You're not on a team.", ephemeral=True)
        return
    if str(team.get("owner_id")) != str(ctx.author.id):
        await ctx.send(f"❌ Only the owner of **{team['name']}** can remove drivers.",
                       ephemeral=True)
        return
    if str(driver.id) == str(ctx.author.id):
        await ctx.send("❌ You can't kick yourself — use `/teamleave` "
                       "(ownership passes to the next member).", ephemeral=True)
        return

    member = next((m for m in team.get("members", [])
                   if str(m.get("discord_id")) == str(driver.id)), None)
    if not member:
        await ctx.send(f"❌ {driver.mention} isn't on **{team['name']}**.", ephemeral=True)
        return

    team["members"] = [m for m in team["members"]
                       if str(m.get("discord_id")) != str(driver.id)]
    set_driver_team(reg, str(driver.id), None)
    save_reg(reg)
    recalc_team_points()
    await apply_team_role(ctx.guild, driver, team, add=False)

    await ctx.send(f"✅ **{member['driver_name']}** has been removed from **{team['name']}**. "
                   f"Roster: {len(team['members'])}/{TEAM_MAX_MEMBERS}")

    staff_ch = discord.utils.get(ctx.guild.text_channels, name=STAFF_CH)
    if staff_ch:
        await staff_ch.send(
            f"🚫 **{member['driver_name']}** removed from **{team['name']}** by {ctx.author}")


@bot.hybrid_command(name="teamdisband", description="Disband your team (owner only)")
@has_arca()
async def team_disband_cmd(ctx):
    reg  = load_reg()
    team = get_team_of(reg, str(ctx.author.id))
    if not team:
        await ctx.send("You're not on a team.", ephemeral=True)
        return
    if str(team.get("owner_id")) != str(ctx.author.id):
        await ctx.send(f"❌ Only the owner of **{team['name']}** can disband it. "
                       f"Use `/teamleave` to leave instead.", ephemeral=True)
        return

    await ctx.send(
        f"⚠️ Disband **{team['name']}**?\n"
        f"This removes all {len(team.get('members', []))} member(s), deletes the team role, "
        f"and wipes **{team.get('points', 0)} team points**. This cannot be undone.",
        view=ConfirmDisbandView(team["name"], str(ctx.author.id)),
        ephemeral=True)


@bot.hybrid_command(name="jointeam", description="Join a team that's recruiting")
@has_arca()
async def join_team_cmd(ctx, *, team_name: str = ""):
    """Join a team that has marked itself as recruiting. Teams not recruiting
    require an invite from the owner via /teaminvite."""
    if not team_name:
        await ctx.send("Usage: `/jointeam <Team Name>` — see `/teams` for the list.",
                       ephemeral=True)
        return

    reg        = load_reg()
    discord_id = str(ctx.author.id)

    driver = next((d for d in reg["drivers"]
                   if str(d.get("discord_id")) == discord_id), None)
    if not driver:
        await ctx.send("❌ You're not registered as a driver yet. Head to `#registration` first.",
                       ephemeral=True)
        return

    team = get_team_in(reg, team_name)
    if not team:
        await ctx.send(f"❌ No team named **{team_name}** found. Use `/teams` to see them all.",
                       ephemeral=True)
        return
    if get_team_of(reg, discord_id):
        await ctx.send(f"⚠️ You're already on **{driver.get('team')}**. Use `/teamleave` first.",
                       ephemeral=True)
        return
    if team_is_full(team):
        await ctx.send(f"❌ **{team['name']}** is full "
                       f"({TEAM_MAX_MEMBERS}/{TEAM_MAX_MEMBERS}).", ephemeral=True)
        return
    if not team.get("looking"):
        owner = team.get("owner_tag", "the owner")
        await ctx.send(f"🔒 **{team['name']}** isn't open for direct joins. "
                       f"Ask {owner} to invite you with `/teaminvite`.", ephemeral=True)
        return

    join_race = load_data().get("race_number", 1)
    team["members"].append({
        "driver_name": driver["name"],
        "discord_id":  discord_id,
        "discord_tag": str(ctx.author),
        "joined_race": join_race,
    })
    set_driver_team(reg, discord_id, team["name"])
    save_reg(reg)
    recalc_team_points()
    await apply_team_role(ctx.guild, ctx.author, team, add=True)

    await ctx.send(
        f"✅ **{driver['name']}** has joined **{team['name']}**! "
        f"Roster: {len(team['members'])}/{TEAM_MAX_MEMBERS}\n"
        f"Team points count from Race {join_race} forward. 🏁")

    staff_ch = discord.utils.get(ctx.guild.text_channels, name=STAFF_CH)
    if staff_ch:
        await staff_ch.send(
            f"🤝 **{driver['name']}** joined **{team['name']}** via /jointeam "
            f"— {len(team['members'])}/{TEAM_MAX_MEMBERS}")


class ConfirmRescoreView(discord.ui.View):
    """Preview-then-apply guard on /fixrace. Rewriting championship points
    with real money on the line shouldn't be one click."""
    def __init__(self, race_num: int, invoker_id: int):
        super().__init__(timeout=180)
        self.race_num = race_num
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Not your command.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Apply Correction", style=discord.ButtonStyle.danger, emoji="🛠")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        changes, corrected = rescore_race(data, self.race_num)
        if not corrected:
            await interaction.response.edit_message(
                content=f"❌ No stored results found for Race {self.race_num}.", view=None)
            return
        save_data(data)
        try:
            recalc_team_points()
        except Exception as e:
            print(f"⚠️ team recalc after rescore failed: {e}")

        lines = [f"**{i+1}.** {r['name']} — {r['race_pts']}"
                 + (f" +{r['stage_pts']}S" if r['stage_pts'] else "")
                 + (" ⚡" if r['fastest_lap_bonus'] else "")
                 + f" = **{r['total_pts']}**"
                 for i, r in enumerate(corrected[:15])]
        embed = discord.Embed(
            title=f"✅ Race {self.race_num} Re-scored",
            description="\n".join(lines) + (f"\n…and {len(corrected)-15} more"
                                            if len(corrected) > 15 else ""),
            color=0x2ECC71)
        embed.add_field(name="Drivers corrected", value=str(len(changes)), inline=True)
        embed.add_field(name="Winner", value=f"{corrected[0]['name']} — "
                                             f"{corrected[0]['total_pts']} pts", inline=True)
        embed.set_footer(text="Standings updated · run /standings to verify")
        await interaction.response.edit_message(content=None, embed=embed, view=None)

        staff = discord.utils.get(interaction.guild.text_channels, name=STAFF_CH)
        if staff:
            await staff.send(
                f"🛠 **Race {self.race_num} re-scored** by {interaction.user} — "
                f"{len(changes)} driver(s) corrected. Winner {corrected[0]['name']} "
                f"now {corrected[0]['total_pts']} pts.\n"
                f"⚠️ **Pull in the desktop app before your next push**, or its older "
                f"copy of data.json will overwrite this.")

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Correction cancelled.",
                                                embed=None, view=None)


@bot.hybrid_command(name="teamaudit", description="Show exactly how each team's points are calculated (admin)")
@is_admin()
async def team_audit_cmd(ctx, team_name: str = ""):
    """Break down team scoring driver by driver.

    Team totals are opaque when they look wrong — a driver can score zero
    for their team because their name didn't match the results, or because
    they joined after the race. This shows which, per driver.
    """
    data = load_data()
    reg  = load_reg()
    race_results    = data.get("race_results", {})
    races_completed = max(0, data.get("race_number", 1) - 1)

    teams = reg.get("teams", [])
    if team_name:
        teams = [t for t in teams
                 if team_name.strip().lower() in (t.get("name", "") or "").lower()]
    if not teams:
        await ctx.send("No matching teams found.", ephemeral=True)
        return

    for team in teams[:5]:
        lines, total, problems = [], 0, []
        for m in team.get("members", []):
            dname = m.get("driver_name", "")
            join  = m.get("joined_race", 1)
            key   = resolve_result_key(dname, race_results)
            if not key:
                lines.append(f"❓ **{dname}** — no results found (joined R{join})")
                if any(_name_tokens(dname) & _name_tokens(k) for k in race_results):
                    problems.append(f"{dname}: name doesn't match results")
                continue
            all_races = race_results.get(key, [])
            counted   = [r for r in all_races if r.get("race", 0) >= join]
            skipped   = [r for r in all_races if r.get("race", 0) < join]
            scores    = [r.get("points", 0) for r in counted]
            window    = max(0, races_completed - (join - 1))
            adj, raw, dropped, _ = adjusted_driver_total(scores, window)
            total += adj
            alias = f" *(matched to {key})*" if key != dname else ""
            note  = ""
            if skipped:
                lost = sum(r.get("points", 0) for r in skipped)
                note = f" · ⚠️ R{','.join(str(r['race']) for r in skipped)} not counted (-{lost}, joined R{join})"
                problems.append(f"{dname}: {lost} pts excluded — joined at Race {join}")
            if dropped:
                note += f" · dropped {dropped}"
            lines.append(f"• **{dname}**{alias} — {adj} pts{note}")

        embed = discord.Embed(
            title=f"🔍 Team Audit — {team.get('name','?')}",
            description="\n".join(lines) or "No members.",
            color=0xF1C40F)
        embed.add_field(name="Calculated total", value=f"**{total}** pts", inline=True)
        embed.add_field(name="Stored total",
                        value=str(team.get("points", 0)), inline=True)
        if problems:
            embed.add_field(name="⚠️ Issues", value="\n".join(problems[:6]), inline=False)
        embed.set_footer(text="Run /recalcteams to rebuild totals after fixing")
        await ctx.send(embed=embed)


@bot.hybrid_command(name="setteamjoin", description="Set a team's join race so earlier results count (admin)")
@is_admin()
async def set_team_join_cmd(ctx, team_name: str, race_number: int):
    """Backdate every member's joined_race for a team.

    Team points count from each driver's join race forward (Rulebook 5.2.2).
    A team formed after Race 1 therefore scores zero from it — correct by
    the rule, but wrong if the team existed and simply wasn't registered in
    the bot until later. This backdates them.
    """
    reg  = load_reg()
    team = get_team_in(reg, team_name)
    if not team:
        matches = [t for t in reg.get("teams", [])
                   if team_name.strip().lower() in (t.get("name", "") or "").lower()]
        team = matches[0] if len(matches) == 1 else None
    if not team:
        await ctx.send(f"❌ No single team matched '{team_name}'. Use `/teams` for exact names.",
                       ephemeral=True)
        return

    changed = []
    for m in team.get("members", []):
        old = m.get("joined_race", 1)
        if old != race_number:
            m["joined_race"] = race_number
            changed.append(f"{m.get('driver_name','?')}: R{old} → R{race_number}")
    save_reg(reg)
    recalc_team_points()

    reg2 = load_reg()
    t2 = get_team_in(reg2, team["name"])
    await ctx.send(
        f"✅ **{team['name']}** join race set to **{race_number}** for "
        f"{len(changed)} member(s).\n"
        + ("\n".join(f"• {c}" for c in changed[:8]) if changed else "*(no changes needed)*")
        + f"\n\nTeam total now: **{t2.get('points', 0) if t2 else 0} pts**")


@bot.hybrid_command(name="recalcteams", description="Rebuild all team point totals (admin)")
@is_admin()
async def recalc_teams_cmd(ctx):
    """Force a team points rebuild — use after fixing names or join races."""
    before = {t["name"]: t.get("points", 0) for t in load_reg().get("teams", [])}
    recalc_team_points()
    after = {t["name"]: t.get("points", 0) for t in load_reg().get("teams", [])}

    lines = []
    for name, new in sorted(after.items(), key=lambda kv: kv[1], reverse=True):
        old = before.get(name, 0)
        arrow = f" ({new-old:+d})" if new != old else ""
        lines.append(f"**{name}** — {new} pts{arrow}")
    await ctx.send(embed=discord.Embed(
        title="🔄 Team Points Rebuilt",
        description="\n".join(lines) or "No teams.",
        color=0x2ECC71))


@bot.hybrid_command(name="fixrace", description="Re-score a past race's points (admin)")
@is_admin()
async def fix_race_cmd(ctx, race_number: int):
    """Recompute a posted race's points from its stored finishing order.

    Fixes the off-by-one that scored Race 1's winner as second place. The
    finishing ORDER stays exactly as recorded — only the points attached to
    each position are recalculated.
    """
    data = load_data()
    preview = json.loads(json.dumps(data))       # deep copy, preview only
    changes, corrected = rescore_race(preview, race_number)

    if not corrected:
        await ctx.send(f"❌ No stored results found for Race {race_number}. "
                       f"Nothing to re-score.", ephemeral=True)
        return

    if not changes:
        await ctx.send(f"✅ Race {race_number} already scores correctly — "
                       f"no changes needed.", ephemeral=True)
        return

    lines = []
    for name, old, new in changes[:15]:
        arrow = "🔺" if new > old else "🔻"
        lines.append(f"{arrow} **{name}** {old} → **{new}** ({new-old:+d})")

    embed = discord.Embed(
        title=f"🛠 Preview — Re-score Race {race_number}",
        description="\n".join(lines) + (f"\n…and {len(changes)-15} more"
                                        if len(changes) > 15 else ""),
        color=0xF1C40F)
    embed.add_field(name="Corrected winner",
                    value=f"{corrected[0]['name']} — {corrected[0]['total_pts']} pts",
                    inline=False)
    embed.set_footer(text="Nothing has changed yet — press Apply to commit.")
    await ctx.send(embed=embed, view=ConfirmRescoreView(race_number, ctx.author.id))


@bot.hybrid_command(name="freeagents", description="Drivers available to recruit — not on a team")
@has_arca()
async def free_agents_cmd(ctx):
    """Who's available to recruit. Team owners live in this list."""
    reg = load_reg()
    # Self-heal any drift before reporting — this list is what owners recruit
    # from, so showing someone as free when they just joined a team is the
    # single most confusing failure mode it has.
    if reconcile_driver_teams(reg):
        save_reg(reg)
    free = [d for d in reg.get("drivers", [])
            if not d.get("team") and d.get("status") not in ("Withdrawn",)]

    if not free:
        await ctx.send("🏁 No free agents right now — every registered driver is on a team.")
        return

    def sort_key(d):
        n = norm_num(d.get("number", ""))
        try:
            return (int(n), n)
        except ValueError:
            return (9999, n)

    confirmed = sorted([d for d in free if d.get("status") == "Confirmed"], key=sort_key)
    other     = sorted([d for d in free if d.get("status") != "Confirmed"], key=sort_key)

    data      = load_data()
    standings = compute_adjusted_standings(data)

    embed = discord.Embed(
        title="🔍 Free Agents — Available to Recruit",
        description="Drivers not currently on a team. Owners: invite with "
                    "`/teaminvite @driver`.",
        color=0x2ECC71,
    )

    def add_group(label, group):
        if not group:
            return
        lines = []
        for d in group:
            pts = standings.get(d["name"], {}).get("points")
            pts_txt = f" — {pts} pts" if pts is not None else ""
            lines.append(f"**#{d.get('number','?')}** {d['name']}{pts_txt}")
        chunk, size, part = [], 0, 1
        for line in lines:
            if (size + len(line) + 1 > 1000 or len(chunk) >= 20) and chunk:
                embed.add_field(name=f"{label} ({len(group)})" if part == 1 else f"{label} (cont.)",
                                value="\n".join(chunk), inline=False)
                chunk, size, part = [], 0, part + 1
            chunk.append(line); size += len(line) + 1
        if chunk:
            embed.add_field(name=f"{label} ({len(group)})" if part == 1 else f"{label} (cont.)",
                            value="\n".join(chunk), inline=False)

    add_group("✅ Confirmed", confirmed)
    add_group("⏳ Pending / Waitlist", other)

    open_teams = [t for t in reg.get("teams", [])
                  if t.get("looking") and len(t.get("members", [])) < TEAM_MAX_MEMBERS]
    if open_teams:
        embed.add_field(
            name="🏎️ Teams Recruiting",
            value="\n".join(
                f"**{t['name']}** — {len(t.get('members', []))}/{TEAM_MAX_MEMBERS} "
                f"(`/jointeam {t['name']}`)" for t in open_teams[:10]),
            inline=False)

    embed.set_footer(text=f"{len(free)} free agent(s) · QSR High Horsepower Series")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="syncgarages", description="Create missing team voice channels (admin)")
@is_admin()
async def sync_garages_cmd(ctx):
    """Backfill private voice channels for teams created before garages
    existed. Safe to re-run — teams that already have one are skipped."""
    reg   = load_reg()
    teams = reg.get("teams", [])
    if not teams:
        await ctx.send("No teams registered yet.")
        return

    await ctx.send(f"🔧 Checking {len(teams)} team(s) for garages…")
    created, skipped, failed = [], [], []

    for team in teams:
        vc_id = team.get("voice_channel_id")
        if vc_id and ctx.guild.get_channel(int(vc_id)):
            skipped.append(team["name"])
            continue
        vc = await ensure_team_voice(ctx.guild, team)
        if vc:
            team["voice_channel_id"] = str(vc.id)
            created.append(team["name"])
        else:
            failed.append(team["name"])
        await asyncio.sleep(1)   # stay clear of Discord's channel-create rate limit

    save_reg(reg)

    msg = ""
    if created: msg += f"✅ **Created {len(created)}:** {', '.join(created)}\n"
    if skipped: msg += f"⏭️ **Already had one ({len(skipped)}):** {', '.join(skipped)}\n"
    if failed:  msg += f"❌ **Failed ({len(failed)}):** {', '.join(failed)}\n"
    await ctx.send(msg or "Nothing to do.")


@bot.hybrid_command(name="teams", description="Team standings and rosters")
@has_arca()
async def teams_cmd(ctx):
    """List all registered teams and their points."""
    reg = load_reg()
    recalc_team_points()
    reg = load_reg()
    teams = sorted(reg["teams"], key=lambda t: t.get("points", 0), reverse=True)
    if not teams:
        await ctx.send("No teams registered yet. Be the first — hit **Register a Team** in `#registration`! 🏁")
        return
    embed = discord.Embed(
        title="🏎️ QSR High Horsepower Series — Team Standings",
        color=0xE8272A,
        timestamp=datetime.utcnow(),
    )
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines  = []
    for i, team in enumerate(teams, 1):
        icon    = medals.get(i, f"`{i:>2}.`")
        members = ", ".join(m["driver_name"] for m in team.get("members", []))
        lines.append(f"{icon} **{team['name']}** — {team.get('points',0)} pts\n"
                     f"    👥 {members}")
    embed.description = "\n".join(lines)
    embed.set_footer(text="Points accumulate from the race each driver joined their team")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="numbers", description="Show available and taken car numbers")
@has_arca()
async def numbers_cmd(ctx):
    """Show available and taken car numbers."""
    taken = taken_numbers()
    avail = [n for n in VALID_NUMBERS if n not in taken]

    embed = discord.Embed(
        title="🔢 QSR Car Numbers — Season 1",
        color=0xE8272A,
    )
    taken_str = " · ".join(f"~~{n}~~" for n in sorted(taken, key=lambda x: (len(x), x))) if taken else "None taken yet!"
    avail_str = " · ".join(avail[:40])
    if len(avail) > 40:
        avail_str += f" _...and {len(avail)-40} more_"

    embed.add_field(name=f"✅ Available ({len(avail)})", value=avail_str or "—", inline=False)
    embed.add_field(name=f"🔴 Taken ({len(taken)})",    value=taken_str,         inline=False)
    embed.set_footer(text="Register in #registration to claim your number")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="roster", description="Full driver roster — everyone signed up and their car number")
@has_arca()
async def roster_cmd(ctx):
    """Quick list of every registered driver and their car number, grouped
    by status and sorted low-to-high."""
    reg = load_reg()
    drivers = [d for d in reg["drivers"] if d.get("status") != "Withdrawn"]
    if not drivers:
        await ctx.send("No drivers registered yet. Head to `#registration` to be the first! 🏁")
        return

    def sort_key(d):
        n = norm_num(d.get("number", ""))
        try:
            return (int(n), n)
        except ValueError:
            return (9999, n)

    confirmed = sorted([d for d in drivers if d["status"] == "Confirmed"], key=sort_key)
    waitlist  = sorted([d for d in drivers if d["status"] == "Waitlist"],  key=sort_key)
    other     = sorted([d for d in drivers if d["status"] not in ("Confirmed", "Waitlist")], key=sort_key)

    embed = discord.Embed(
        title="🏁 QSR High Horsepower Series — Driver Roster",
        color=0xE8272A,
    )

    def add_group(label: str, group: list):
        if not group:
            return
        lines = [f"**#{d.get('number','?')}** — {d['name']}"
                 + (f" ({d['team']})" if d.get("team") else "")
                 for d in group]
        chunk, chunk_len, part = [], 0, 1
        for line in lines:
            if (chunk_len + len(line) + 1 > 1000 or len(chunk) >= 20) and chunk:
                embed.add_field(
                    name=f"{label} ({len(group)})" if part == 1 else f"{label} (cont.)",
                    value="\n".join(chunk), inline=False)
                chunk, chunk_len, part = [], 0, part + 1
            chunk.append(line)
            chunk_len += len(line) + 1
        if chunk:
            embed.add_field(
                name=f"{label} ({len(group)})" if part == 1 else f"{label} (cont.)",
                value="\n".join(chunk), inline=False)

    add_group("✅ Confirmed", confirmed)
    add_group("📋 Waitlist", waitlist)
    add_group("⏳ Other", other)

    embed.set_footer(text=f"{len(drivers)} driver(s) on the roster · {reg.get('max_field',40)} max field")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="mystats", description="Your registration profile and team")
@has_arca()
async def mystats_cmd(ctx):
    """Check your own registration status and team."""
    discord_id = str(ctx.author.id)
    driver = get_driver_reg(discord_id)
    if not driver:
        await ctx.send(
            "You're not registered yet! Head to `#registration` and click **Register as Driver**. 🏁")
        return
    embed = discord.Embed(
        title=f"🏁 {driver['name']} — Registration Profile",
        color=0xE8272A,
    )
    embed.add_field(name="Car Number", value=f"#{driver['number']}",   inline=True)
    embed.add_field(name="Status",     value=driver["status"],          inline=True)
    embed.add_field(name="Payment",    value="✅ Paid" if driver.get("paid") else "⏳ Pending", inline=True)
    embed.add_field(name="iRacing ID", value=driver.get("iracing_id","—"), inline=True)
    embed.add_field(name="Team",       value=driver.get("team") or "No team", inline=True)
    embed.set_footer(text="QSR High Horsepower Series Season 1")
    await ctx.send(embed=embed, ephemeral=False)


@bot.hybrid_command(name="mynumber", description="Change your car number")
@has_arca()
async def mynumber_cmd(ctx):
    """Let an already-registered driver swap to a different open car number."""
    discord_id = str(ctx.author.id)
    driver = get_driver_reg(discord_id)
    if not driver:
        await ctx.send(
            "You're not registered yet! Head to `#registration` and click "
            "**Register as Driver** first. 🏁", ephemeral=True)
        return

    if driver.get("status") == "Withdrawn":
        await ctx.send(
            "Your registration is marked **Withdrawn** — contact an admin "
            "before changing numbers.", ephemeral=True)
        return

    if not available_numbers():
        await ctx.send("❌ Every other car number is currently taken. Contact an admin.",
                       ephemeral=True)
        return

    await ctx.send(
        f"🔁 **Change your car number.** You're currently **#{driver['number']}**.\n"
        f"Pick your new number below — only open numbers are shown.",
        view=ChangeNumberView(), ephemeral=True)


# ─────────────────────────────────────────────────────────────────
#  ROLE SELECTION — #get-roles dropdown
# ─────────────────────────────────────────────────────────────────

class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)  # Persistent — never expires

    @discord.ui.select(
        custom_id="role_select",
        placeholder="🏁 Select your roles...",
        min_values=0,
        max_values=1,
        options=[
            discord.SelectOption(
                label="ARCA Series",
                value="arca",
                description="Get pinged for race announcements and series updates",
                emoji="🏁"
            ),
        ]
    )
    async def role_select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        guild      = interaction.guild
        arca_role  = guild.get_role(ARCA_ROLE_ID)

        if not arca_role:
            await interaction.response.send_message(
                "⚠️ Role not found — contact an admin.", ephemeral=True
            )
            return

        member     = interaction.user
        has_arca   = arca_role in member.roles
        added      = []
        removed    = []

        if "arca" in select.values:
            if not has_arca:
                await member.add_roles(arca_role)
                added.append("ARCA Series 🏁")
        else:
            if has_arca:
                await member.remove_roles(arca_role)
                removed.append("ARCA Series 🏁")

        if added and removed:
            msg = f"✅ Added: {', '.join(added)}\n❌ Removed: {', '.join(removed)}"
        elif added:
            msg = f"✅ You've been given the **{', '.join(added)}** role! You'll now get race announcements."
        elif removed:
            msg = f"❌ Removed: **{', '.join(removed)}**"
        else:
            msg = "No changes made."

        await interaction.response.send_message(msg, ephemeral=True)


@bot.hybrid_command(name="setuproles", description="Post the role selection dropdown in #get-roles (admin)")
@is_admin()
async def setup_roles_cmd(ctx):
    """Post the role selection dropdown in #get-roles. Run once."""
    guild = ctx.guild
    ch    = discord.utils.get(guild.text_channels, name="get-roles")
    if not ch:
        await ctx.send("❌ #get-roles channel not found. Create it first.")
        return
    embed = discord.Embed(
        title="🏁 QSR Simulations — Get Your Roles",
        description=(
            "Select your roles below to get notified for the things that matter to you.\n\n"
            "**🏁 ARCA Series** — Race announcements, series updates, green flag pings\n\n"
            "You can add or remove roles at any time by using this menu again."
        ),
        color=0xE8272A
    )
    embed.set_footer(text="QSR Simulations | More roles coming in future seasons")
    await ch.send(embed=embed, view=RoleSelectView())
    await ctx.send("✅ Role selection posted in #get-roles!")




@bot.command(name="loadschedule")
@is_admin()
async def load_schedule(ctx):
    if not ctx.message.attachments:
        await ctx.send("📎 Attach a CSV with columns: `Track,Date`")
        return
    raw    = await ctx.message.attachments[0].read()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    data   = load_data()
    data["schedule"] = [{"track": r["Track"], "date": r["Date"], "complete": False} for r in reader]
    save_data(data)
    await ctx.send(f"✅ Schedule loaded — {len(data['schedule'])} races.")

@bot.command(name="restructure")
@is_admin()
async def restructure(ctx):
    NEW_STRUCTURE = {
        "📋 FRONT DESK": ["welcome", "get-roles", "announcements", "qsr-record-book"],
        "🏁 QSR HIGH HORSEPOWER SERIES": [
            "series-announcements", "schedule", "points-standings", "race-results",
            "league-rules", "penalty-report", "number-list", "number-request", "registration"
        ],
        "💬 COMMUNITY": [
            "pitlane", "ask-dale", "media-share", "racing-irl",
            "meme-central", "hot-takes", "nascar-fan-chat", "qsr-race-polls"
        ],
        "📺 BROADCAST & EVENTS": [
            "how-to-watch", "hosted-sessions", "qsr-live", "league-socials", "team-forming",
            "dales-post-race", "dales-sportsbook"
        ],
        "🔒 STAFF ONLY": ["staff-chat", "staff-docs"],
    }
    await ctx.send("⚙️ Restructuring server... ~60 seconds.")
    guild    = ctx.guild
    existing = {ch.name: ch for ch in guild.channels}
    for cat_name, channels in NEW_STRUCTURE.items():
        category = discord.utils.get(guild.categories, name=cat_name)
        if not category:
            category = await guild.create_category(cat_name)
        for ch_name in channels:
            if ch_name not in existing:
                await guild.create_text_channel(ch_name, category=category)
            else:
                await existing[ch_name].edit(category=category)
        await asyncio.sleep(1)
    await ctx.send("✅ Server restructured! Manually delete any old channels you no longer need.")

@bot.command(name="setupsportsbook")
@is_admin()
async def setup_sportsbook(ctx):
    """One-off setup: creates #dales-sportsbook (under 📺 BROADCAST & EVENTS,
    same as !restructure would) and drops a pinned how-to-play post. Safe to
    run more than once — reuses the channel/category if they already exist."""
    guild = ctx.guild
    cat_name = "📺 BROADCAST & EVENTS"
    category = discord.utils.get(guild.categories, name=cat_name)
    if not category:
        category = await guild.create_category(cat_name)

    ch = discord.utils.get(guild.text_channels, name=SPORTSBOOK_CHANNEL)
    if not ch:
        ch = await guild.create_text_channel(SPORTSBOOK_CHANNEL, category=category)
        created = True
    else:
        if ch.category != category:
            await ch.edit(category=category)
        created = False

    embed = discord.Embed(
        title="🎰 Welcome to Dale's Book",
        description=(
            "For-fun Dale Dollars sportsbook — no real money, no real stakes. "
            "Season's top bankroll wins free entry to the next series.\n\n"
            "**The book opens every Tuesday** (the day after race night) and stays "
            "open all week, locking at lobby-up on the next race night."
        ),
        color=0x2ECC71,
    )
    embed.add_field(
        name="Commands",
        value="`/odds` — this week's board\n"
              "`/wager` — place a pick\n"
              "`/balance` — your Dale Dollars + open bets\n"
              "`/moneyboard` — season leaderboard",
        inline=False,
    )
    embed.add_field(
        name="Season prize eligibility",
        value=f"You need to have placed a bet in **{MIN_RACES_FOR_PRIZE}+ races** to be "
              f"eligible for the season-end prize — sitting on your starting ${STARTING_BALANCE} "
              f"without playing doesn't count. Check your progress with `/balance`.",
        inline=False,
    )
    embed.set_footer(text="Dale Dollars have no cash value and can't be purchased.")
    msg = await ch.send(embed=embed)
    try:
        await msg.pin()
    except discord.HTTPException:
        pass

    await ctx.send(
        f"{'✅ Created' if created else 'ℹ️ Already existed —'} #{SPORTSBOOK_CHANNEL}"
        f"{' and pinned the how-to-play post.' if created else ', posted a fresh how-to-play there.'}"
    )

@bot.hybrid_command(name="career", description="Driver career summary + race-by-race history")
@has_arca()
async def career_cmd(ctx, *, driver_name: str = ""):
    """
    !career <Driver Name>
    Shows a driver's career summary + race-by-race history.
    Available to everyone.

    NOTE: bot.py reads its own local data.json on Railway.
    This command works once data.json is synced from Race Control
    (via git push, shared DB, or manual upload). Until then it reads
    whatever data Railway has available.
    """
    data            = load_data()
    standings = compute_adjusted_standings(data)
    race_results    = data.get("race_results", {})
    driver_profiles = data.get("driver_profiles", {})

    if not driver_name:
        await ctx.send("Usage: `!career <Driver Name>`\nExample: `!career Norman King`")
        return

    # Fuzzy match
    matched = next((n for n in standings if driver_name.lower() in n.lower()), None)
    if not matched:
        await ctx.send(f"❌ Driver `{driver_name}` not found in standings.")
        return

    info    = standings[matched]
    history = race_results.get(matched, [])
    profile = driver_profiles.get(matched, {})

    # ── Derived stats ──────────────────────────────────────────────
    races       = info.get("races", 0)
    wins        = info.get("wins", 0)
    total_pts   = info.get("points", 0)
    total_inc   = info.get("incidents", 0)
    top5s       = sum(1 for r in history if r["finish"] <= 5)
    top10s      = sum(1 for r in history if r["finish"] <= 10)
    avg_finish  = round(sum(r["finish"] for r in history) / len(history), 1) if history else "—"
    best_finish = min((r["finish"] for r in history), default=None)
    avg_inc     = round(total_inc / races, 1) if races else "—"
    clean_runs  = sum(1 for r in history if r["incidents"] == 0)

    # Championship position
    sorted_s    = standings_sorted(standings)
    champ_pos   = next((i + 1 for i, (n, _) in enumerate(sorted_s) if n == matched), "?")
    leader_pts  = sorted_s[0][1]["points"] if sorted_s else 0
    gap         = leader_pts - total_pts
    races_run   = data.get("race_number", 1) - 1

    medals   = {1: "🏆", 2: "🥈", 3: "🥉"}
    pos_icon = medals.get(champ_pos, f"P{champ_pos}")

    # ── Embed 1: Career Summary ────────────────────────────────────
    summary_embed = discord.Embed(
        title=f"🏁 {matched} — Career Profile",
        color=0xE8272A,
        timestamp=datetime.utcnow()
    )
    summary_embed.add_field(
        name="📊 Championship",
        value=(
            f"**{pos_icon} Position:** P{champ_pos}\n"
            f"**Points:** {total_pts}\n"
            f"**Gap to Leader:** {'LEADER' if gap == 0 else f'-{gap} pts'}"
        ),
        inline=True
    )
    summary_embed.add_field(
        name="🏎️ Season Stats",
        value=(
            f"**Races:** {races} of {races_run}\n"
            f"**Wins:** {wins}\n"
            f"**Top 5s:** {top5s}\n"
            f"**Top 10s:** {top10s}"
        ),
        inline=True
    )
    summary_embed.add_field(
        name="📈 Averages",
        value=(
            f"**Avg Finish:** {avg_finish}\n"
            f"**Best Finish:** P{best_finish if best_finish else '—'}\n"
            f"**Avg Incidents:** {avg_inc}x\n"
            f"**Clean Runs:** {clean_runs}"
        ),
        inline=True
    )
    srh_url     = profile.get("sim_racer_hub_url", "")
    footer_text = "QSR High Horsepower Series — Season 1"
    if srh_url:
        footer_text += f" | SRH: {srh_url}"
    summary_embed.set_footer(text=footer_text)

    # ── Embed 2: Race-by-Race History ─────────────────────────────
    history_embed = discord.Embed(
        title=f"📋 {matched} — Race History",
        color=0x1A1A2E,
        timestamp=datetime.utcnow()
    )
    if not history:
        history_embed.description = "No race history yet — check back after Race 1! 🏁"
    else:
        lines = []
        for r in history:
            finish_icon = medals.get(r["finish"], f"P{r['finish']:>2}")
            inc_str     = f" ⚠️{r['incidents']}x" if r["incidents"] > 0 else " ✅"
            stage_str   = f" +{r['stage_pts']}S" if r.get("stage_pts") else ""
            lines.append(
                f"**R{r['race']}** {finish_icon} · {r['track'][:22]} · "
                f"{r['points']}{stage_str} pts{inc_str}"
            )
        history_embed.description = "\n".join(lines)
        history_embed.set_footer(text=f"{len(history)} race(s) | Season 1 · QSR High Horsepower Series")

    await ctx.send(embed=summary_embed)
    await ctx.send(embed=history_embed)









# ─────────────────────────────────────────────────────────────────
#  STATS CARD — Pillow-generated driver graphic
#  Broadcast wedge + telemetry readout, 1200x640 PNG, rounded corners.
#  Gradient wedge, glow accents, drop shadows, real QSR logo composite.
#  Visual language matches generate_winner_graphic() in qsr_app.py.
# ─────────────────────────────────────────────────────────────────

_SC_FONT_DIRS = [
    "/usr/share/fonts/opentype/bebas-neue",
    "/usr/share/fonts/truetype/dejavu",
    "/usr/share/fonts/truetype/liberation",
]
_SC_FONT_NAMES = {
    # Bebas Neue first for "bold" — condensed display font used for every
    # headline/hero number on the card (name, position, points, car #).
    # Falls back to DejaVu/Liberation bold if the apt package isn't
    # installed, so the card never breaks — it just looks plainer.
    "bold":    ["BebasNeue-Bold.otf", "DejaVuSans-Bold.ttf",     "LiberationSans-Bold.ttf"],
    "regular": ["DejaVuSans.ttf",          "LiberationSans-Regular.ttf"],
    "mono":    ["DejaVuSansMono.ttf",      "LiberationMono-Regular.ttf"],
    "monob":   ["DejaVuSansMono-Bold.ttf", "LiberationMono-Bold.ttf"],
}
_SC_FONT_CACHE = {}

def _sc_font(size, weight="bold"):
    """Load a scalable font at an exact size. Cached. Never raises —
    falls back to the default bitmap font if system fonts are missing."""
    key = (size, weight)
    if key in _SC_FONT_CACHE:
        return _SC_FONT_CACHE[key]
    from PIL import ImageFont
    for d in _SC_FONT_DIRS:
        for name in _SC_FONT_NAMES.get(weight, _SC_FONT_NAMES["bold"]):
            p = os.path.join(d, name)
            if os.path.exists(p):
                f = ImageFont.truetype(p, size)
                _SC_FONT_CACHE[key] = f
                return f
    try:
        f = ImageFont.load_default(size=size)
    except TypeError:          # Pillow < 9.2 has no size kwarg
        f = ImageFont.load_default()
    _SC_FONT_CACHE[key] = f
    return f

def _sc_tw(d, t, f):
    b = d.textbbox((0, 0), t, font=f); return b[2] - b[0]

def _sc_th(d, t, f):
    b = d.textbbox((0, 0), t, font=f); return b[3] - b[1]

def _sc_auto(d, t, max_w, start, weight="bold", min_size=12, step=2):
    """Largest size <= start that fits t within max_w."""
    s = start
    while s > min_size:
        f = _sc_font(s, weight)
        if _sc_tw(d, t, f) <= max_w:
            return f
        s -= step
    return _sc_font(min_size, weight)

def _sc_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None

def _sc_gradient(w, h, c1, c2, x0, y0, x1, y1):
    """Linear gradient projected along an arbitrary direction."""
    import numpy as np
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dx, dy = x1 - x0, y1 - y0
    length2 = max(1.0, dx * dx + dy * dy)
    t = ((xx - x0) * dx + (yy - y0) * dy) / length2
    t = np.clip(t, 0, 1)
    c1 = np.array(c1, dtype=np.float32); c2 = np.array(c2, dtype=np.float32)
    out = c1[None, None, :] + t[:, :, None] * (c2 - c1)[None, None, :]
    return out.astype("uint8")

def _sc_radial_glow(w, h, cx, cy, radius, color, max_alpha):
    import numpy as np
    from PIL import Image
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / max(1, radius)
    a = np.clip(1 - dist, 0, 1) ** 1.8 * max_alpha
    layer = np.zeros((h, w, 4), dtype="uint8")
    layer[:, :, 0], layer[:, :, 1], layer[:, :, 2] = color
    layer[:, :, 3] = a.astype("uint8")
    return Image.fromarray(layer, "RGBA")

def _sc_shadow_text(img, xy, text, font, fill, blur=4, offset=(0, 3), alpha=110,
                    shadow_color=(0, 0, 0)):
    """Draw text with a soft drop shadow for depth."""
    from PIL import Image, ImageDraw, ImageFilter
    x, y = xy
    d = ImageDraw.Draw(img)
    bbox = d.textbbox((0, 0), text, font=font)
    pad = blur * 3 + 2
    layer = Image.new("RGBA", (bbox[2] - bbox[0] + pad * 2, bbox[3] - bbox[1] + pad * 2), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    ld.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=(*shadow_color, alpha))
    if blur > 0:
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
    img.paste(layer, (x - pad + offset[0], y - pad + offset[1]), layer)
    ImageDraw.Draw(img).text((x, y), text, font=font, fill=fill)

def _sc_strip_dark_bg(img, threshold=35):
    """Remove near-black pixels from a logo for transparent composite —
    same technique as _wg_strip_dark_bg in qsr_app.py's winner graphic."""
    from PIL import Image
    import numpy as np
    img = img.convert("RGBA")
    arr = np.array(img)
    r, g, b, a = arr[:, :, 0], arr[:, :, 1], arr[:, :, 2], arr[:, :, 3]
    mask = (r < threshold) & (g < threshold) & (b < threshold)
    arr[:, :, 3] = np.where(mask, 0, a)
    return Image.fromarray(arr)

def _sc_rounded_mask(w, h, r):
    from PIL import Image, ImageDraw
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=r, fill=255)
    return m


def generate_statscard(driver_name: str, car_number: str, champ_pos: int,
                       total_pts: int, gap: int, wins: int, top5s: int,
                       top10s: int, races: int, avg_finish, best_finish,
                       avg_inc, clean_runs: int, recent_finishes: list,
                       archetype: str = "",
                       pts_trend: int = None,
                       races_remaining: int = 0,
                       nemesis: str = "",
                       nemesis_record: str = "") -> bytes:
    """
    Generate a 1200x640 driver stats card. Returns PNG bytes (b"" on failure).
    recent_finishes: finish positions, MOST RECENT FIRST.
    """
    try:
        from PIL import Image, ImageDraw, ImageFilter
    except ImportError:
        return b""

    W, H = 1200, 640
    RADIUS   = 22
    BG       = (7,   7,   8)
    BG2      = (13,  13,  14)
    PANEL    = (16,  16,  17)
    PANEL_HI = (21,  21,  22)
    ORANGE   = (237, 92,  17)
    ORANGE2  = (255, 138, 40)
    ORANGE_D = (150, 52,   6)
    GOLD     = (255, 200, 40)
    WHITE    = (248, 248, 248)
    DIM      = (140, 144, 148)
    DIM2     = (88,  92,  96)
    LINE     = (36,  36,  38)
    GREEN    = (86,  214, 130)
    RED      = (240, 95,  95)

    base = _sc_gradient(W, H, BG, BG2, 0, 0, 0, H)
    img  = Image.fromarray(base, "RGB").convert("RGBA")
    d    = ImageDraw.Draw(img)

    for x in range(0, W, 40):
        d.line([(x, 0), (x, H)], fill=(13, 13, 14))
    for y in range(0, H, 40):
        d.line([(0, y), (W, y)], fill=(13, 13, 14))

    # ══ WEDGE ═══════════════════════════════════════════════════════
    cut_top, cut_bot = 340, 250

    wedge_grad = _sc_gradient(cut_top + 40, H, (255, 150, 60), (200, 58, 8), 0, 0, cut_top, H)
    wedge_layer = Image.fromarray(wedge_grad, "RGB").convert("RGBA")
    wmask = Image.new("L", (cut_top + 40, H), 0)
    ImageDraw.Draw(wmask).polygon([(0, 0), (cut_top, 0), (cut_bot, H), (0, H)], fill=255)
    img.paste(wedge_layer, (0, 0), wmask)
    d = ImageDraw.Draw(img)

    for gx in range(0, cut_top, 68):
        d.line([(gx, 0), (gx, H)], fill=ORANGE_D, width=1)
    for gy in range(0, H, 68):
        d.line([(0, gy), (cut_top, gy)], fill=ORANGE_D, width=1)

    # Ghost car-number watermark — dark-orange on the gradient, drawn
    # BEFORE the dark panel so the panel/seam naturally clip whatever
    # bleeds past the wedge. Stays a background layer, never a bright
    # foreground number — the driver identity is the hero, not the digit.
    GHOST = (200, 80, 20)
    num_str = str(car_number) if car_number not in (None, "") else "?"
    gf = _sc_font(300, "bold")
    gb = d.textbbox((0, 0), num_str, font=gf)
    gw, gh = gb[2] - gb[0], gb[3] - gb[1]
    d.text((cut_top // 2 - gw // 2 - gb[0], H // 2 - gh // 2 - gb[1] + 40),
           num_str, font=gf, fill=GHOST)

    pmask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(pmask).polygon(
        [(cut_top - 26, 0), (W, 0), (W, H), (cut_bot - 26, H)], fill=255)
    full_panel = Image.fromarray(
        _sc_gradient(W, H, (19, 19, 20), (9, 9, 10), 0, 0, 0, H), "RGB").convert("RGBA")
    img.paste(full_panel, (0, 0), pmask)
    d = ImageDraw.Draw(img)

    d.polygon([(cut_top - 10, 0), (cut_top + 6, 0),
               (cut_bot + 6, H), (cut_bot - 10, H)], fill=ORANGE)

    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for i, a in enumerate([100, 65, 38, 18, 8]):
        off = 6 + i * 8
        gd.polygon([(cut_top + off, 0), (cut_top + off + 8, 0),
                    (cut_bot + off, H), (cut_bot + off - 8, H)], fill=(255, 120, 45, a))
    img.paste(glow, (0, 0), glow)
    d = ImageDraw.Draw(img)

    tl_glow = _sc_radial_glow(W, H, 60, 20, 420, (255, 190, 110), 70)
    img.paste(tl_glow, (0, 0), tl_glow)
    d = ImageDraw.Draw(img)

    # ── foreground wedge content — the hero layer ───────────────────
    _sc_shadow_text(img, (48, 44), "CAR", _sc_font(16, "monob"), WHITE, blur=3, offset=(0, 2), alpha=120)
    car_lbl = f"#{num_str}"
    _sc_shadow_text(img, (48, 64), car_lbl, _sc_font(30, "bold"), WHITE, blur=5, offset=(0, 3), alpha=150)
    d = ImageDraw.Draw(img)
    d.rectangle([48, 104, 48 + 120, 107], fill=GOLD)

    _sc_shadow_text(img, (44, 320), "CHAMPIONSHIP", _sc_font(14, "monob"), WHITE, blur=3, offset=(0, 2), alpha=110)
    d = ImageDraw.Draw(img)
    pos_str = f"P{champ_pos}" if champ_pos else "—"
    posf = _sc_auto(d, pos_str, 230, 108, "bold", 48)
    d.text((40, 342), pos_str, font=posf, fill=(12, 9, 6))

    if archetype:
        at = archetype.upper()
        _sc_shadow_text(img, (44, 508), at, _sc_auto(d, at, 200, 17, "monob", 10),
                        WHITE, blur=3, offset=(0, 2), alpha=110)
        d = ImageDraw.Draw(img)
    d.text((44, 536), "QSR HHPS  //  SEASON 1", font=_sc_font(12, "mono"), fill=(230, 230, 230))

    for ly, al in [(18, 140), (34, 90), (50, 45)]:
        ln = Image.new("RGBA", (W, 1), (0, 0, 0, 0))
        ImageDraw.Draw(ln).line([(cut_top + 40, 0), (W, 0)], fill=(*ORANGE, al), width=1)
        img.paste(ln, (0, ly), ln)
    d = ImageDraw.Draw(img)

    # ══ RIGHT SIDE ════════════════════════════════════════════════
    X0, XR = 400, W - 44
    RW = XR - X0

    # QSR logo, top right — real PNG if the repo has it, dark-strip
    # composited with a soft backing glow. Silently skipped if missing.
    #
    # Candidate order matters: _DATA_DIR is Railway's persistent /data
    # volume — that's where data.json etc. live, but it is NOT where a
    # git-committed file like the logo ends up. The repo checkout lives
    # next to bot.py itself, so that's resolved via __file__ first — this
    # works regardless of the container's working directory, which a bare
    # relative filename does not.
    LOGO_W = 138
    logo_ok = False
    _sc_module_dir = os.path.dirname(os.path.abspath(__file__))
    logo_candidates = [
        os.path.join(_sc_module_dir, "qsr_league_logo.png"),
        os.path.join(_sc_module_dir, "B9C7F26D6B0347F5B38F8895CF7A17DC.png"),
        os.path.join(_DATA_DIR, "qsr_league_logo.png"),
        os.path.join(_DATA_DIR, "B9C7F26D6B0347F5B38F8895CF7A17DC.png"),
        "qsr_league_logo.png",
        "B9C7F26D6B0347F5B38F8895CF7A17DC.png",
    ]
    _sc_logo_found = next((p for p in logo_candidates if os.path.exists(p)), None)
    if not _sc_logo_found:
        print(f"[statscard] logo not found. Checked: {logo_candidates}")
    for logo_path in logo_candidates:
        if os.path.exists(logo_path):
            try:
                raw = Image.open(logo_path).convert("RGBA")
                clean = _sc_strip_dark_bg(raw, threshold=35)
                lh = int(clean.height * (LOGO_W / clean.width))
                clean = clean.resize((LOGO_W, lh), Image.LANCZOS)
                lx, ly = XR - LOGO_W, 34
                lglow = _sc_radial_glow(W, H, lx + LOGO_W // 2, ly + lh // 2, 110, (255, 140, 60), 55)
                img.paste(lglow, (0, 0), lglow)
                d = ImageDraw.Draw(img)
                img.paste(clean, (lx, ly), clean)
                d = ImageDraw.Draw(img)
                logo_ok = True
                print(f"[statscard] logo loaded from {logo_path}")
            except Exception as e:
                logo_ok = False
                print(f"[statscard] logo found at {logo_path} but failed to composite: {e}")
            break

    name_max = RW - (LOGO_W + 30) if logo_ok else RW
    nm = _sc_auto(d, driver_name.upper(), name_max, 58, "bold", 24)
    _sc_shadow_text(img, (X0, 44), driver_name.upper(), nm, WHITE, blur=4, offset=(0, 3), alpha=110)
    d = ImageDraw.Draw(img)
    ub = 44 + _sc_th(d, driver_name.upper(), nm) + 16
    d.rectangle([X0, ub, XR, ub + 4], fill=ORANGE)
    ubg = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ubg).rectangle([X0, ub + 4, XR, ub + 10], fill=(*ORANGE, 60))
    img.paste(ubg, (0, 0), ubg)
    d = ImageDraw.Draw(img)

    # ── hero readouts ─────────────────────────────────────────────
    hy = ub + 26
    if gap == 0:
        gap_v, gap_c = "LEADER", GOLD
    else:
        gap_v, gap_c = f"-{gap}", DIM
    if pts_trend is None:
        tr_v, tr_c = "—", DIM2
    else:
        tr_v = f"+{pts_trend}" if pts_trend >= 0 else str(pts_trend)
        tr_c = GREEN if pts_trend >= 0 else RED

    cells = [("POINTS", str(total_pts), WHITE),
             ("GAP", gap_v, gap_c),
             ("LAST RACE", tr_v, tr_c)]
    cw = RW // 3
    for i, (lbl, val, col) in enumerate(cells):
        cx = X0 + i * cw
        d.text((cx, hy), lbl, font=_sc_font(12, "monob"), fill=DIM2)
        vf = _sc_auto(d, val, cw - 24, 52, "bold", 20)
        _sc_shadow_text(img, (cx, hy + 20), val, vf, col, blur=5, offset=(0, 3), alpha=100)
        d = ImageDraw.Draw(img)
        if i:
            d.line([(cx - 16, hy), (cx - 16, hy + 68)], fill=LINE)

    # ── trace chart ───────────────────────────────────────────────
    CY, CH = hy + 90, 240
    CW = int(RW * 0.58)
    CX = X0
    d.rounded_rectangle([CX, CY, CX + CW, CY + CH], radius=10, fill=PANEL, outline=LINE)
    d.text((CX + 16, CY + 14), "FINISH POSITION TRACE", font=_sc_font(12, "monob"), fill=DIM)

    trace = [p for p in (recent_finishes or []) if isinstance(p, int)][:5][::-1]
    px0, py0 = CX + 52, CY + 46
    pw_, ph_ = CW - 72, CH - 74
    if len(trace) >= 2:
        maxp = max(max(trace), 20)
        span = max(1, maxp - 1)
        for gv in sorted({1, maxp // 2, maxp}):
            gy = py0 + (gv - 1) / span * ph_
            d.line([(px0, gy), (px0 + pw_, gy)], fill=(26, 26, 27))
            d.text((CX + 16, gy - 7), f"P{gv}", font=_sc_font(10, "mono"), fill=DIM2)
        n = len(trace)
        co = [(px0 + (i / (n - 1)) * pw_, py0 + (pv - 1) / span * ph_) for i, pv in enumerate(trace)]
        glow_line = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        gld = ImageDraw.Draw(glow_line)
        gld.line(co, fill=(*ORANGE, 90), width=8, joint="curve")
        glow_line = glow_line.filter(ImageFilter.GaussianBlur(4))
        img.paste(glow_line, (0, 0), glow_line)
        d = ImageDraw.Draw(img)
        d.line(co, fill=ORANGE2, width=3, joint="curve")
        for (x, y), pv in zip(co, trace):
            c = GOLD if pv == 1 else ORANGE2 if pv <= 5 else WHITE
            d.ellipse([x - 5, y - 5, x + 5, y + 5], fill=(9, 9, 10), outline=c, width=3)
            lf = _sc_font(11, "monob")
            d.text((x - _sc_tw(d, str(pv), lf) / 2, y - 24), str(pv), font=lf, fill=c)
    elif len(trace) == 1:
        pv = trace[0]
        cxm, cym = CX + CW // 2, CY + CH // 2 + 8
        d.text((CX + 16, CY + 34), "1 RACE ON THE BOARD", font=_sc_font(11, "mono"), fill=DIM2)
        c = GOLD if pv == 1 else ORANGE2 if pv <= 5 else WHITE
        rglow = _sc_radial_glow(W, H, cxm, cym, 70, c, 70)
        img.paste(rglow, (0, 0), rglow)
        d = ImageDraw.Draw(img)
        big = _sc_font(46, "bold")
        label = f"P{pv}"
        lw = _sc_tw(d, label, big)
        d.ellipse([cxm - 46, cym - 46, cxm + 46, cym + 46], outline=c, width=3)
        d.text((cxm - lw / 2, cym - 26), label, font=big, fill=c)
        msg = "Trace builds as races come in"
        mf = _sc_font(12, "regular")
        d.text((cxm - _sc_tw(d, msg, mf) / 2, cym + 58), msg, font=mf, fill=DIM2)
    else:
        nf = _sc_font(14, "monob")
        d.text((CX + (CW - _sc_tw(d, "NO RACE DATA", nf)) / 2, CY + CH / 2 - 8),
               "NO RACE DATA", font=nf, fill=DIM2)

    # ── stat cards ────────────────────────────────────────────────
    BX = CX + CW + 20
    stats = [("WINS", wins, GOLD if wins > 0 else WHITE),
             ("TOP 5", top5s, WHITE),
             ("TOP 10", top10s, WHITE),
             ("CLEAN", clean_runs, GREEN if clean_runs > 0 else WHITE)]
    sh = (CH - 12 * 3) // 4
    sy = CY
    for lbl, v, c in stats:
        d.rounded_rectangle([BX, sy, XR, sy + sh], radius=8, fill=PANEL_HI, outline=LINE)
        d.text((BX + 14, sy + sh // 2 - 16), lbl, font=_sc_font(11, "monob"), fill=DIM)
        vf = _sc_font(24, "bold")
        _sc_shadow_text(img, (XR - 14 - _sc_tw(d, str(v), vf), sy + sh // 2 - 14),
                        str(v), vf, c, blur=4, offset=(0, 2), alpha=90)
        d = ImageDraw.Draw(img)
        sy += sh + 12

    # ── secondary readouts ───────────────────────────────────────
    ry2 = CY + CH + 16
    inc_f = _sc_float(avg_inc)
    ic = DIM2 if inc_f is None else (RED if inc_f >= 3 else GREEN)
    best_str = f"P{best_finish}" if best_finish else "—"
    trio = [("AVG FIN", str(avg_finish), WHITE), ("AVG INC", str(avg_inc), ic),
            ("BEST", best_str, ORANGE2 if best_finish == 1 else WHITE)]
    tcw = RW // 3
    for i, (lbl, val, col) in enumerate(trio):
        cx = X0 + i * tcw
        d.text((cx, ry2), lbl, font=_sc_font(10, "monob"), fill=DIM2)
        d.text((cx, ry2 + 16), val, font=_sc_font(16, "monob"), fill=col)

    # ── season meter ──────────────────────────────────────────────
    sy2 = ry2 + 46
    remaining = max(0, min(14, races_remaining or 0))
    run = 14 - remaining
    d.text((X0, sy2), "SEASON PROGRESS", font=_sc_font(11, "monob"), fill=DIM)

    max_avail = remaining * 66          # 55 race + 10 stage + 1 fastest lap
    if remaining == 0:
        st_t, st_c = "SEASON COMPLETE", DIM
    elif gap == 0:
        st_t, st_c = f"LEADING  //  {remaining} TO DEFEND", GOLD
    elif gap <= max_avail:
        st_t, st_c = f"ALIVE  //  {max_avail} AVAILABLE", GREEN
    else:
        st_t, st_c = f"ELIMINATED  //  {max_avail} LEFT", RED
    stf = _sc_font(11, "monob")
    d.text((XR - _sc_tw(d, st_t, stf), sy2 - 24), st_t, font=stf, fill=st_c)

    d.rounded_rectangle([X0, sy2 + 18, XR, sy2 + 30], radius=6, fill=(20, 20, 21), outline=(32, 32, 34))
    fw = int(RW * run / 14)
    if fw > 10:
        mgl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        mgld = ImageDraw.Draw(mgl)
        mgld.rounded_rectangle([X0, sy2 + 16, X0 + fw, sy2 + 32], radius=8, fill=(*ORANGE, 60))
        mgl = mgl.filter(ImageFilter.GaussianBlur(3))
        img.paste(mgl, (0, 0), mgl)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([X0, sy2 + 18, X0 + fw, sy2 + 30], radius=6, fill=ORANGE)
    for i in range(1, 14):
        tx = X0 + int(RW * i / 14)
        d.line([(tx, sy2 + 18), (tx, sy2 + 30)], fill=(9, 9, 10))

    # ══ TICKER ════════════════════════════════════════════════════
    TB = H - 64
    d.rectangle([0, TB, W, H], fill=(11, 11, 12))
    d.rectangle([0, TB, W, TB + 3], fill=ORANGE)
    d.text((44, TB + 25), "RECENT FORM", font=_sc_font(13, "monob"), fill=DIM)

    bx = 220
    chips = [p for p in (recent_finishes or []) if isinstance(p, int)][:5]
    if chips:
        for pos in chips:
            c = (GOLD if pos == 1 else ORANGE2 if pos <= 3 else
                 (150, 110, 40) if pos <= 5 else
                 (40, 90, 70) if pos <= 10 else (34, 34, 36))
            d.rounded_rectangle([bx, TB + 14, bx + 52, TB + 50], radius=7, fill=c)
            pf = _sc_font(17, "monob")
            d.text((bx + (52 - _sc_tw(d, str(pos), pf)) / 2, TB + 23), str(pos),
                   font=pf, fill=((10, 10, 10) if pos <= 3 else WHITE))
            bx += 60
    else:
        d.text((bx, TB + 25), "NO RACES YET", font=_sc_font(13, "mono"), fill=DIM2)

    ft  = f"QSR SIMULATIONS  //  {datetime.utcnow().strftime('%Y.%m.%d')}"
    ftf = _sc_font(11, "mono")
    ftx = XR - _sc_tw(d, ft, ftf)
    d.text((ftx, TB + 27), ft, font=ftf, fill=DIM2)

    if nemesis:
        rv = f"RIVAL  //  {nemesis}" + (f"  {nemesis_record}" if nemesis_record else "")
        rvf = _sc_auto(d, rv, max(60, ftx - bx - 40), 12, "mono", 9)
        d.text((bx + 20, TB + 27), rv, font=rvf, fill=DIM2)

    # ── round the corners + thin outer edge ──────────────────────
    mask = _sc_rounded_mask(W, H, RADIUS)
    out = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    ImageDraw.Draw(out).rounded_rectangle([0, 0, W - 1, H - 1], radius=RADIUS,
                                          outline=(0, 0, 0, 120), width=2)

    final = Image.new("RGB", (W, H), (5, 5, 5))
    final.paste(out, (0, 0), out)

    buf = io.BytesIO()
    final.save(buf, format="PNG", optimize=True)
    buf.seek(0)
    return buf.getvalue()

@bot.hybrid_command(name="statscard", aliases=["card", "stats"], description="Driver stats graphic — yours or any driver's")
@has_arca()
async def statscard_cmd(ctx, *, driver_name: str = ""):
    """
    !statscard             — your own card
    !statscard <Name>      — any driver's card
    !card / !stats         — aliases
    """
    data         = load_data()
    standings    = data.get("standings", {})
    race_results = data.get("race_results", {})

    # Resolve driver name
    if not driver_name:
        # Look up by Discord ID from registration
        reg_driver = get_driver_reg(str(ctx.author.id))
        if reg_driver:
            driver_name = reg_driver["name"]
        else:
            await ctx.send(
                "You're not registered. Use `!statscard <Driver Name>` or "
                "register in `#registration` first.")
            return

    matched = next((n for n in standings if driver_name.lower() in n.lower()), None)
    if not matched:
        await ctx.send(
            f"❌ No stats found for `{driver_name}`. "
            "They may not have raced yet or the name doesn't match standings.")
        return

    info    = standings[matched]
    history = race_results.get(matched, [])

    # ── Derive stats ─────────────────────────────────────────
    races       = info.get("races", 0)
    wins        = info.get("wins", 0)
    total_pts   = info.get("points", 0)
    total_inc   = info.get("incidents", 0)
    top5s       = sum(1 for r in history if r["finish"] <= 5)
    top10s      = sum(1 for r in history if r["finish"] <= 10)
    avg_finish  = round(sum(r["finish"] for r in history) / len(history), 1) if history else "—"
    best_finish = min((r["finish"] for r in history), default=None)
    avg_inc     = round(total_inc / races, 1) if races else "—"
    clean_runs  = sum(1 for r in history if r["incidents"] == 0)
    recent      = [r["finish"] for r in sorted(history, key=lambda r: r["race"], reverse=True)[:5]]

    sorted_s  = standings_sorted(standings)
    champ_pos = next((i + 1 for i, (n, _) in enumerate(sorted_s) if n == matched), 0)
    leader_pts = sorted_s[0][1]["points"] if sorted_s else 0
    gap        = leader_pts - total_pts

    # Get car number from registration
    reg     = load_reg()
    reg_drv = next((d for d in reg["drivers"]
                    if d["name"].lower() == matched.lower()), None)
    car_num = reg_drv["number"] if reg_drv else "?"

    # Points trend
    pts_trend = None
    sorted_hist = sorted(history, key=lambda r: r["race"])
    if len(sorted_hist) >= 2:
        last_pts = sorted_hist[-1]["points"] + sorted_hist[-1].get("stage_pts", 0)
        prev_pts = sorted_hist[-2]["points"] + sorted_hist[-2].get("stage_pts", 0)
        pts_trend = last_pts - prev_pts
    elif len(sorted_hist) == 1:
        pts_trend = sorted_hist[-1]["points"] + sorted_hist[-1].get("stage_pts", 0)

    # Championship math
    races_run       = data.get("race_number", 1) - 1
    races_remaining = max(0, 14 - races_run)

    # Nemesis from rivalries.json
    nemesis = ""
    nemesis_record = ""
    rivalries = load_rivalries()
    worst = None
    worst_ratio = 0
    for key, rv in rivalries.items():
        if matched not in rv.get("drivers", []):
            continue
        a, b  = rv["drivers"]
        other = b if a == matched else a
        my_w  = rv["wins"].get(matched, 0)
        opp_w = rv["wins"].get(other, 0)
        total = my_w + opp_w
        if total < 2:
            continue
        ratio = opp_w / total
        if ratio > worst_ratio:
            worst_ratio = ratio
            worst = (other, my_w, opp_w)
    if worst:
        nemesis, my_w, opp_w = worst
        nemesis_record = f"{my_w}-{opp_w}"

    await ctx.typing()

    archetypes = get_driver_archetypes(race_results, standings)
    archetype  = archetypes.get(matched, "")

    img_bytes = generate_statscard(
        driver_name=matched,
        car_number=car_num,
        champ_pos=champ_pos,
        total_pts=total_pts,
        gap=gap,
        wins=wins,
        top5s=top5s,
        top10s=top10s,
        races=races,
        avg_finish=avg_finish,
        best_finish=best_finish,
        avg_inc=avg_inc,
        clean_runs=clean_runs,
        recent_finishes=recent,
        archetype=archetype,
        pts_trend=pts_trend,
        races_remaining=races_remaining,
        nemesis=nemesis,
        nemesis_record=nemesis_record,
    )

    if not img_bytes:
        await ctx.send("⚠️ Could not generate graphic — Pillow may not be installed on Railway.")
        return

    filename = f"statscard_{matched.replace(' ', '_')}.png"
    await ctx.send(
        file=discord.File(fp=io.BytesIO(img_bytes), filename=filename))


@bot.hybrid_command(name="rivalries", aliases=["beef", "h2h"], description="Hottest head-to-head rivalries this season")
@has_arca()
async def rivalries_cmd(ctx):
    """Show the hottest current rivalries in the series."""
    data         = load_data()
    standings    = data.get("standings", {})
    race_results = data.get("race_results", {})
    rivalries    = load_rivalries()

    if not rivalries:
        await ctx.send("No rivalry data yet — check back after a few races. 🏁")
        return

    # Sort by heat score, filter to pairs with 2+ races together
    hot = sorted(
        [(k, v) for k, v in rivalries.items() if v.get("races_together", 0) >= 2],
        key=lambda x: x[1].get("heat", 0),
        reverse=True
    )[:5]

    if not hot:
        await ctx.send("Not enough head-to-head data yet. Race more. 🏁")
        return

    archetypes = get_driver_archetypes(race_results, standings)

    embed = discord.Embed(
        title="⚔️ QSR Rivalry Report",
        color=0xE8520A,
        timestamp=datetime.utcnow()
    )

    lines = []
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"]
    for i, (key, rv) in enumerate(hot):
        a, b   = rv["drivers"]
        wa, wb = rv["wins"].get(a, 0), rv["wins"].get(b, 0)
        arch_a = archetypes.get(a, "")
        arch_b = archetypes.get(b, "")
        arch_str = ""
        if arch_a or arch_b:
            arch_str = f" *({arch_a} vs {arch_b})*" if arch_a and arch_b else ""
        heat_bar = "🔥" * min(5, max(1, rv.get("heat", 0) // 20))
        lines.append(
            f"{medals[i]} **{a}** {wa}–{wb} **{b}**{arch_str}\n"
            f"  {heat_bar} · {rv.get('races_together', 0)} races · "
            f"{rv.get('closer_finishes', 0)} close battles"
        )

    embed.description = "\n\n".join(lines)
    embed.set_footer(text="Heat score rises when drivers battle close. QSR High Horsepower Series.")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="archetypes", aliases=["types", "drivers"], description="Every driver's current archetype label")
@has_arca()
async def archetypes_cmd(ctx):
    """Show every driver's current archetype label."""
    data         = load_data()
    standings    = data.get("standings", {})
    race_results = data.get("race_results", {})

    archetypes = get_driver_archetypes(race_results, standings)
    if not archetypes:
        await ctx.send("No archetype data yet — need at least 2 races per driver. 🏁")
        return

    sorted_s = standings_sorted(standings)
    icons = {
        "The Hotshot":  "⚡",
        "The Wrecker":  "💥",
        "The Ironman":  "🛡️",
        "The Closer":   "🎯",
        "The Wildcard": "🃏",
        "The Grinder":  "⚙️",
    }
    lines = []
    for driver, info in sorted_s:
        arch = archetypes.get(driver)
        if arch:
            icon = icons.get(arch, "🏎️")
            lines.append(f"{icon} **{driver}** — *{arch}* · {info['points']} pts")

    embed = discord.Embed(
        title="🏎️ Driver Archetypes — Season 1",
        description="\n".join(lines) or "No data yet.",
        color=0xE8520A,
        timestamp=datetime.utcnow()
    )
    embed.set_footer(text="Archetypes update after every race. QSR High Horsepower Series.")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  DALE'S BOOK — no-stakes prediction game
# ─────────────────────────────────────────────────────────────────

@bot.hybrid_command(name="odds", description="Dale's Book — this week's for-fun odds board")
async def odds_cmd(ctx):
    data  = load_data()
    board = data.get("odds_board") or {}
    if not board.get("moneyline"):
        await ctx.send("Dale's Book opens Tuesday, the day after race night. Check back then. 🎰")
        return
    await ctx.send(embed=format_odds_embed(board))


@bot.hybrid_command(name="postodds", description="Manually build and post this week's odds board (admin)")
@is_admin()
async def post_odds_cmd(ctx):
    """Manual trigger for testing — race_prediction() only builds the board
    automatically at 7PM ET on race day. This runs the exact same
    build_odds_board() path on demand, so /wager can be tested any time
    without waiting for that window. If the board fails its own
    consistency check it is NOT posted — same rule as the automatic path."""
    data = load_data()
    reg  = load_reg()
    board, problems = build_odds_board(data, reg)
    if problems:
        await ctx.send(
            "⚠️ Board failed validation, not posted:\n" + "\n".join(f"• {p}" for p in problems),
            ephemeral=True)
        return
    save_data(data)
    await ctx.send(embed=format_odds_embed(board))


def team_manufacturers(reg: dict, data: dict, team_name: str, exclude_discord_id: str) -> set:
    """Known manufacturers raced by a driver's teammates (excluding
    themselves) — used so a manufacturer bet can't indirectly favor a
    teammate's car the same way a direct moneyline bet on them would."""
    if not team_name:
        return set()
    out = set()
    for d in reg.get("drivers", []):
        if d.get("team") != team_name or str(d.get("discord_id")) == exclude_discord_id:
            continue
        m = data.get("driver_profiles", {}).get(d.get("name"), {}).get("manufacturer")
        if m:
            out.add(m)
    return out


@bot.hybrid_command(name="wager", description="Place a for-fun Dale Dollars wager on this week's board")
@discord.app_commands.describe(
    bet_type="What you're betting on",
    pick="Driver name (moneyline/top5/top10), manufacturer name, or 'over'/'under' for a prop",
    amount="Dale Dollars to risk — capped by how likely the pick is",
    double_down="Use your one Power Play boost for this race (raises the cap, not manufacturer)",
)
@discord.app_commands.choices(bet_type=[
    discord.app_commands.Choice(name="Race Winner (moneyline)", value="moneyline"),
    discord.app_commands.Choice(name="Top 5 Finish", value="top5"),
    discord.app_commands.Choice(name="Top 10 Finish", value="top10"),
    discord.app_commands.Choice(name="Manufacturer Winner (max $5)", value="manufacturer"),
    discord.app_commands.Choice(name="Leader finishing position O/U", value="leader_ou"),
    discord.app_commands.Choice(name="Total cautions O/U", value="cautions_ou"),
    discord.app_commands.Choice(name="Total lead changes O/U", value="lead_changes_ou"),
])
async def wager_cmd(ctx, bet_type: str, pick: str, amount: int, double_down: bool = False):
    data  = load_data()
    reg   = load_reg()
    board = data.get("odds_board") or {}
    if not board.get("open") or not board.get("moneyline"):
        await ctx.send("Dale's Book isn't open right now — it runs Tuesday through lobby-up on race night. Check `/odds`.")
        return
    race_num = board["race_number"]
    did      = str(ctx.author.id)

    econ = data.setdefault("economy", {"balances": {}, "history": {}, "double_down_used": {}})
    dd_used = econ.setdefault("double_down_used", {})
    if double_down and race_num in dd_used.get(did, []):
        await ctx.send("You've already used this week's Power Play boost.")
        return

    bettor_team = team_of(reg, did)
    bet_record  = {"discord_id": did, "name": ctx.author.display_name,
                   "double_down": double_down, "settled": False, "won": None,
                   "placed_at": datetime.utcnow().isoformat()}
    prob = 0.5   # default for the flat-odds O/U props

    if bet_type in ("moneyline", "top5", "top10"):
        target = resolve_driver_by_name(reg, pick)
        if not target:
            await ctx.send(f"Couldn't find a confirmed driver matching \"{pick}\". Check `/roster`.")
            return
        target_id   = str(target.get("discord_id", ""))
        target_team = target.get("team")
        if target_id == did:
            await ctx.send("Can't bet on yourself — pick someone else. 🚫")
            return
        if bettor_team and target_team and bettor_team == target_team:
            await ctx.send("Can't bet on a teammate — that's a conflict of interest. 🚫")
            return
        market = board.get(bet_type, board.get("moneyline"))
        priced = market.get(target["name"])
        if not priced:
            await ctx.send(f"{target['name']} isn't priced on this week's board yet.")
            return
        prob = priced["prob"]
        bet_record.update(type=bet_type, target=target["name"], odds=priced["american"])

    elif bet_type == "manufacturer":
        manu_market = board.get("manufacturer") or {}
        if not manu_market:
            note = board.get("manufacturer_note", "not enough manufacturer data on file this week")
            await ctx.send(f"Manufacturer betting isn't available this week — {note}.")
            return
        matched = next((m for m in manu_market if m.lower() == pick.strip().lower()), None)
        if not matched:
            await ctx.send(f"\"{pick}\" isn't one of this week's manufacturers: {', '.join(manu_market)}.")
            return
        own_manu  = manufacturer_of(reg, data, did)
        team_manu = team_manufacturers(reg, data, bettor_team, did)
        if own_manu and matched == own_manu:
            await ctx.send("Can't bet on your own manufacturer. 🚫")
            return
        if matched in team_manu:
            await ctx.send("Can't bet on a teammate's manufacturer. 🚫")
            return
        prob = manu_market[matched]["prob"]
        bet_record.update(type="manufacturer", target=matched, odds=manu_market[matched]["american"])

    elif bet_type in ("leader_ou", "cautions_ou", "lead_changes_ou"):
        side = pick.strip().lower()
        if side not in ("over", "under"):
            await ctx.send("For O/U props, pick must be `over` or `under`.")
            return
        prop = next((p for p in board.get("props", []) if p["id"] == bet_type), None)
        if not prop:
            await ctx.send("That prop isn't on the board this week.")
            return
        if bet_type == "leader_ou":
            leader_id   = str(next((d.get("discord_id", "") for d in reg.get("drivers", [])
                                     if d.get("name") == prop.get("driver")), ""))
            leader_team = team_of(reg, leader_id) if leader_id else None
            if leader_id == did:
                await ctx.send("Can't bet on your own finishing position. 🚫")
                return
            if bettor_team and leader_team and bettor_team == leader_team:
                await ctx.send("Can't bet on a teammate's finishing position. 🚫")
                return
        bet_record.update(type=bet_type, side=side, line=prop["line"], odds=prop["odds"])
    else:
        await ctx.send("Unknown bet type.")
        return

    # Stake cap — the harder the pick, the more you're allowed to risk.
    # Manufacturer always caps at $5 regardless of Power Play.
    if bet_type == "manufacturer":
        cap = MANUFACTURER_MAX_STAKE
    else:
        cap = stake_cap_for_prob(prob)
        if double_down:
            cap = max(cap, 20)

    if amount < MIN_STAKE or amount > cap:
        await ctx.send(f"That pick's cap is **${cap}** this week (min ${MIN_STAKE}). Try an amount in that range.")
        return

    ensure_balance(data, did)
    if econ["balances"][did] < amount:
        await ctx.send(f"Not enough Dale Dollars — you've got ${econ['balances'][did]}, this bet needs ${amount}.")
        return

    bet_record["stake"] = amount
    econ["balances"][did] -= amount
    if double_down:
        dd_used.setdefault(did, []).append(race_num)
    data.setdefault("bets", {}).setdefault(str(race_num), []).append(bet_record)
    save_data(data)

    label = bet_record.get("target") or f"{bet_record['side']} {bet_record.get('line','')}"
    await ctx.send(
        f"✅ Bet placed: **${amount}** on **{label}** at **{bet_record['odds']}**"
        f"{' 🔥 (Power Play)' if double_down else ''}. Balance: ${econ['balances'][did]}."
    )


@bot.hybrid_command(name="balance", description="Check your Dale Dollars balance")
async def balance_cmd(ctx):
    data = load_data()
    did  = str(ctx.author.id)
    bal  = ensure_balance(data, did)
    save_data(data)
    board = data.get("odds_board") or {}
    open_bets = [b for b in data.get("bets", {}).get(str(board.get("race_number")), [])
                 if b.get("discord_id") == did and not b.get("settled")]
    embed = discord.Embed(title="💵 Your Dale Dollars", color=0x2ECC71)
    embed.add_field(name="Balance", value=f"${bal}", inline=True)
    races = races_bet_on(data, did)
    elig  = "✅ Eligible" if races >= MIN_RACES_FOR_PRIZE else f"{races}/{MIN_RACES_FOR_PRIZE} races"
    embed.add_field(name="Season prize eligibility", value=elig, inline=True)
    if open_bets:
        lines = [f"${b['stake']} {b['type']} — {b.get('target') or (b.get('side','')+' '+str(b.get('line','')))} @ {b['odds']}"
                 for b in open_bets]
        embed.add_field(name="Open bets this week", value="\n".join(lines), inline=False)
    await ctx.send(embed=embed)


@bot.hybrid_command(name="moneyboard", description="Season-long Dale Dollars leaderboard")
async def moneyboard_cmd(ctx):
    data = load_data()
    bal  = data.get("economy", {}).get("balances", {})
    reg  = load_reg()
    id_to_name = {str(d.get("discord_id")): d.get("name") for d in reg.get("drivers", [])}
    if not bal:
        await ctx.send("Nobody's placed a bet yet. Type `/odds` to see this week's board.")
        return

    ranked = sorted(bal.items(), key=lambda kv: kv[1], reverse=True)
    eligible, ineligible = [], []
    for did, amt in ranked:
        races = races_bet_on(data, did)
        (eligible if races >= MIN_RACES_FOR_PRIZE else ineligible).append((did, amt, races))

    lines = [f"**{i+1}.** {id_to_name.get(did, f'<@{did}>')} — ${amt}"
             for i, (did, amt, _races) in enumerate(eligible[:15])]
    embed = discord.Embed(
        title="🏆 Dale's Book — Season Money Board",
        description="\n".join(lines) or
                     f"Nobody's eligible yet — place bets in {MIN_RACES_FOR_PRIZE}+ races to qualify.",
        color=0xFFD700,
    )
    if ineligible:
        watch = sorted(ineligible, key=lambda t: -t[1])[:5]
        watch_lines = [f"{id_to_name.get(did, f'<@{did}>')} — ${amt} ({races}/{MIN_RACES_FOR_PRIZE} races)"
                       for did, amt, races in watch]
        embed.add_field(name=f"Not yet eligible (< {MIN_RACES_FOR_PRIZE} races bet)",
                        value="\n".join(watch_lines), inline=False)
    embed.set_footer(text=f"Top bankroll among drivers who've bet in {MIN_RACES_FOR_PRIZE}+ races "
                          f"wins free entry to the next series.")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="forcesettle", description="Manually settle a race's bets (admin)")
@is_owner()
async def forcesettle_cmd(ctx, race_number: int):
    data = load_data()
    reg  = load_reg()
    changes = grade_and_settle_race(data, reg, race_number)
    save_data(data)
    if not changes:
        await ctx.send(f"Nothing to settle for Race {race_number} — either no bets or no results posted yet.")
        return
    voided = sum(1 for c in changes if c[3] == "void")
    msg = f"✅ Settled {len(changes)} bet(s) for Race {race_number}."
    if voided:
        msg += f" ({voided} voided/refunded — insufficient data to grade.)"
    await ctx.send(msg)


@bot.hybrid_command(name="help", description="List all Ask Dale commands")
@has_arca()
async def help_cmd(ctx):
    """Driver-facing command list. Admin commands are deliberately excluded —
    members don't need them and listing them just invites failed attempts."""
    ai_status = "✅ AI Enabled" if ANTHROPIC_API_KEY else "⚠️ FAQ Mode"
    embed = discord.Embed(
        title=f"🏁 Ask Dale — Driver Commands [{ai_status}]",
        description="Every command works as a slash command — type `/` and pick it "
                    "from the list. The old `!` versions still work too.",
        color=0xE8272A
    )
    embed.add_field(
        name="🗣️  Ask Dale",
        value="`/ask <anything>` — rules, iRacing, NASCAR history, racing tips\n"
              "`/dale <question>` — same thing",
        inline=False)
    embed.add_field(
        name="🏆  Season Info",
        value="`/standings` — championship standings\n"
              "`/schedule` — full season calendar\n"
              "`/rules` — quick rules summary\n"
              "`/roster` — every driver signed up and their number\n"
              "`/numbers` — which car numbers are open",
        inline=False)
    embed.add_field(
        name="👤  Your Profile",
        value="`/mystats` — your registration, number and team\n"
              "`/mynumber` — change your car number\n"
              "`/career <Name>` — race-by-race history for any driver\n"
              "`/statscard [Name]` — driver stats graphic",
        inline=False)
    embed.add_field(
        name="🏎️  Teams",
        value="`/teams` — team standings and rosters\n"
              "`/myteam` — your roster, points and open slots\n"
              "`/createteam` — start a new team (gets its own voice garage)\n"
              "`/jointeam <Name>` — join a team that's recruiting\n"
              "`/freeagents` — drivers available to recruit\n"
              "`/teamleave` — leave your team",
        inline=False)
    embed.add_field(
        name="👑  Team Owners Only",
        value="`/teaminvite @driver` — invite a driver to your team\n"
              "`/teamkick @driver` — remove a driver\n"
              "`/teamdisband` — disband the team",
        inline=False)
    embed.add_field(
        name="🔥  Storylines",
        value="`/rivalries` — hottest head-to-head battles\n"
              "`/archetypes` — every driver's archetype label",
        inline=False)
    embed.add_field(
        name="🎰  Dale's Book (for fun — no real money)",
        value="`/odds` — this week's board (winner, top5, top10, manufacturer, props)\n"
              "`/wager` — place a pick, amount capped by how likely it is\n"
              "`/balance` — your Dale Dollars\n"
              "`/moneyboard` — season leaderboard",
        inline=False)
    embed.set_footer(text="QSR High Horsepower Series — Season 1")
    await ctx.send(embed=embed)


@bot.command(name="dalemem")
@is_admin()
async def dale_memory_cmd(ctx, *, username: str = ""):
    memory = load_user_memory()
    if "reset" in username.lower() and ctx.message.mentions:
        target = ctx.message.mentions[0]
        uid = str(target.id)
        if uid in memory:
            del memory[uid]
            save_user_memory(memory)
            await ctx.send(f"✅ Dale has forgotten everything about {target.display_name}. Clean slate.")
        else:
            await ctx.send(f"Dale didn't have any memory of {target.display_name} anyway.")
        return
    if not memory:
        await ctx.send("Dale doesn't remember anyone yet.")
        return
    embed = discord.Embed(title="🧠 Dale's User Memory", color=0xE8272A)
    lines = []
    for uid, info in list(memory.items())[-15:]:
        attitude = info.get("attitude", "neutral")
        icon = "😤" if attitude == "rude" else "😊" if attitude == "friendly" else "😐"
        lines.append(f"{icon} **{info['name']}** — {info['interactions']} interactions | {attitude}")
        if info.get("notes"):
            lines.append(f"   └ {info['notes'][-1][:60]}")
    embed.description = "\n".join(lines) or "No users remembered yet."
    embed.set_footer(text="Use !dalemem reset @user to clear someone's memory")
    await ctx.send(embed=embed)

@bot.hybrid_command(name="newcomer", description="Welcome a new driver to the garage (admin)")
@is_admin()
async def newcomer_cmd(ctx, *, driver_name: str):
    guild = bot.get_guild(GUILD_ID)
    await newcomer_callout(guild, driver_name)
    await ctx.send(f"✅ Dale welcomed {driver_name} to the garage!")

@bot.hybrid_command(name="dalerecap", description="Trigger Dale's post-race reaction (admin)")
@is_admin()
async def dale_recap_cmd(ctx):
    data     = load_data()
    history  = data.get("race_history", {})
    race_num = data.get("race_number", 1)
    if not history:
        await ctx.send("No race history yet.")
        return
    last_sub = list(history.keys())[-1]
    results  = history[last_sub].get("results", [])
    guild    = bot.get_guild(GUILD_ID)
    await ctx.send("⏳ Dale is processing the race...")
    await post_race_reaction(guild, race_num - 1, results, last_sub)
    await ctx.send("✅ Dale's reaction posted!")

class ConfirmDuplicatePollView(discord.ui.View):
    """A poll already exists for this race — confirm before posting a
    second one, mirroring the duplicate-post guard used everywhere else
    this season (Race Control's Post Results, /fixrace)."""
    def __init__(self, race_num: int, results: list, ch, invoker_id: int):
        super().__init__(timeout=120)
        self.race_num, self.results, self.ch = race_num, results, ch
        self.invoker_id = invoker_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("Not your command.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Post Anyway", style=discord.ButtonStyle.danger)
    async def post_anyway(self, interaction: discord.Interaction, button: discord.ui.Button):
        ok, msg = await post_weapon_of_week_poll(self.ch, self.race_num, self.results)
        await interaction.response.edit_message(
            content=("✅ " if ok else "⚠️ ") + msg, view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled — no new poll posted.", view=None)


async def post_custom_poll(ch, question: str, option_names: list, duration_hours: int,
                           multiple: bool, race_number=None) -> tuple:
    """Post an arbitrary Dale poll — question + up to 10 options.

    Shares the exact same storage record shape as Weapon of the Week
    (type differs, everything else lines up), so the auto-closer task and
    the app's Polls tab handle it with no extra code: one poll pipeline,
    two ways to fill in the options.
    """
    try:
        question = question.strip()[:300]
        options  = [o.strip()[:55] for o in option_names if o.strip()][:10]
        if len(options) < 2:
            return False, "Need at least 2 non-empty options, separated by `|`."
        if len(set(options)) != len(options):
            return False, "Two options ended up identical after the 55-character trim — make them more distinct."
        duration_hours = max(1, min(768, duration_hours))   # Discord's allowed range

        poll = discord.Poll(question=question, duration=timedelta(hours=duration_hours),
                            multiple=multiple)
        for opt in options:
            poll.add_answer(text=opt)

        msg = await ch.send(poll=poll)

        data = load_data()
        data.setdefault("polls", []).append({
            "id":           f"custom_{int(datetime.utcnow().timestamp())}",
            "type":         "custom",
            "race_number":  race_number,
            "channel_id":   str(ch.id),
            "message_id":   str(msg.id),
            "question":     question,
            "options":      [{"name": o} for o in options],
            "created_at":   datetime.utcnow().isoformat(),
            "updated_at":   datetime.utcnow().isoformat(),
            "closes_at":    (datetime.utcnow() + timedelta(hours=duration_hours)).isoformat(),
            "closed":       False,
            "results":      None,
        })
        save_data(data)
        return True, f"Posted \"{question}\" with {len(options)} option(s), closes in {duration_hours}h."
    except Exception as e:
        return False, f"Failed to post: {e}"


@bot.hybrid_command(name="createpoll", description="Post a custom Dale poll (admin)")
@is_admin()
async def create_poll_cmd(ctx, question: str, options: str,
                          duration_hours: int = 48, multiple: bool = False,
                          channel: discord.TextChannel = None):
    """Post any poll, not just Weapon of the Week.

    options: separate choices with | — e.g. "Daytona | Talladega | Bristol"
    duration_hours: 1 to 768 (32 days), defaults to 48
    multiple: allow voters to pick more than one option
    channel: defaults to wherever you run the command

    Uses the same storage as Weapon of the Week, so it auto-closes on
    schedule and shows up in the app's Polls tab with zero extra setup.
    """
    ch = channel or ctx.channel
    ok, msg = await post_custom_poll(ch, question, options.split("|"),
                                     duration_hours, multiple)
    target = f" in {ch.mention}" if channel else ""
    await ctx.send(("✅ " if ok else "⚠️ ") + msg + target)


@bot.hybrid_command(name="weaponpoll", description="Post the Weapon of the Week poll for a past race (admin)")
@is_admin()
async def weapon_poll_cmd(ctx, race_number: int):
    """Manually post just the incident poll for a race — without re-posting
    Dale's whole text reaction, which /dalerecap would do. Built for exactly
    this: the automatic pipeline posted the reaction already (or ran before
    the poll feature existed), and only the poll needs to go out."""
    data    = load_data()
    record  = data.get("race_history", {}).get(f"race_{race_number}")
    results = record.get("results", []) if record else []
    if not results:
        await ctx.send(f"❌ No stored results for Race {race_number}. "
                       f"Nothing to build a poll from.", ephemeral=True)
        return

    guild = ctx.guild
    ch = discord.utils.get(guild.text_channels, name="dales-post-race")
    if not ch:
        await ctx.send("❌ #dales-post-race channel not found.", ephemeral=True)
        return

    existing = [p for p in data.get("polls", [])
                if p.get("race_number") == race_number
                and p.get("type") == "weapon_of_the_week"]
    if existing:
        state = "closed" if existing[-1].get("closed") else "still open"
        await ctx.send(
            f"⚠️ A Weapon of the Week poll for Race {race_number} already exists "
            f"({state}). Post another one anyway?",
            view=ConfirmDuplicatePollView(race_number, results, ch, ctx.author.id),
            ephemeral=True)
        return

    ok, msg = await post_weapon_of_week_poll(ch, race_number, results)
    await ctx.send(("✅ " if ok else "⚠️ ") + msg)


@bot.hybrid_command(name="dalemood", description="Set or view Dale's mood (admin)")
@is_admin()
async def dale_mood_cmd(ctx, mood: str = ""):
    if mood in ["neutral", "good", "grumpy", "fired_up"]:
        set_dale_mood(mood, "Manually set by admin")
        await ctx.send(f"✅ Dale's mood set to `{mood}`")
    else:
        current = get_dale_mood()
        await ctx.send(f"Dale's current mood: `{current}`\nOptions: `neutral` `good` `grumpy` `fired_up`")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 You don't have permission to use that command.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"⚠️ Error: {error}")

# ─────────────────────────────────────────────────────────────────
#  SYNC SERVER — Flask HTTP endpoints for Windows app sync
#  Runs in a background thread on PORT (default 2424)
#  Protected by SYNC_TOKEN env var
# ─────────────────────────────────────────────────────────────────

sync_app = Flask(__name__)

def check_token(req):
    """Returns True if the request carries the correct sync token."""
    token = req.headers.get("X-Sync-Token", "")
    if not SYNC_TOKEN:
        return False  # No token configured — reject everything
    return token == SYNC_TOKEN

@sync_app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "bot": "QSR Ask Dale"}), 200

@sync_app.route("/sync/data", methods=["GET"])
def get_data():
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    if not os.path.exists(DATA_FILE):
        return jsonify({}), 200
    with open(DATA_FILE) as f:
        return jsonify(json.load(f)), 200

SYNC_LOG_FILE = os.path.join(_DATA_DIR, "sync_log.json")

def log_sync(event: str, detail: dict):
    """Append-only record of every sync push, keeping the last 200.

    Both data-loss incidents this season were diagnosed by guesswork after
    the fact. A push log means the next one is a lookup, not an
    investigation: what arrived, what it would have changed, whether it was
    accepted or blocked."""
    try:
        entries = []
        if os.path.exists(SYNC_LOG_FILE):
            with open(SYNC_LOG_FILE) as f:
                entries = json.load(f)
        entries.append({"ts": datetime.utcnow().isoformat(),
                        "event": event, **detail})
        with open(SYNC_LOG_FILE, "w") as f:
            json.dump(entries[-200:], f, indent=2)
    except Exception as e:
        print(f"⚠️ sync log write failed: {e}")


def guard_destructive(current: dict, payload: dict, force: bool) -> list:
    """Return a list of destructive changes a data.json push would cause.

    The desktop app is always pushing a snapshot, so a stale one can wipe
    real results — the same way a stale registration snapshot deleted six
    drivers and a team. Shrinking standings, shrinking race history, or
    winding the race counter backwards are never legitimate side effects of
    a routine push, so they're blocked unless explicitly forced.
    """
    if force:
        return []
    problems = []

    for key, label in (("standings", "standings entries"),
                       ("race_results", "drivers with race history"),
                       ("driver_profiles", "driver profiles")):
        before = len(current.get(key, {}) or {})
        after  = len(payload.get(key, {}) or {})
        if after < before:
            problems.append(f"{label}: {before} → {after} (would lose {before - after})")

    # Total recorded race finishes across all drivers
    def finish_count(d):
        return sum(len(v or []) for v in (d.get("race_results", {}) or {}).values())
    rb, ra = finish_count(current), finish_count(payload)
    if ra < rb:
        problems.append(f"recorded race finishes: {rb} → {ra} (would lose {rb - ra})")

    cur_rn, new_rn = current.get("race_number", 1), payload.get("race_number", 1)
    if isinstance(new_rn, int) and isinstance(cur_rn, int) and new_rn < cur_rn:
        problems.append(f"race counter would move backwards: {cur_rn} → {new_rn}")

    return problems


@sync_app.route("/sync/data", methods=["POST"])
def post_data():
    """Accept a data.json push — guarded, merged, backed up, and logged.

    Was a straight overwrite for everything except the count-based
    guard_destructive check. That check protects standings/race_results/
    race_number from shrinking, but "polls" and "race_history" got no
    protection at all — a stale app push (pulled before a poll was created,
    or before /fixrace corrected a race) would silently delete the poll or
    revert the correction. Exactly the bug class that hit registration.json's
    teams earlier. Same fix: merge those two by key, server-authoritative on
    existence, rather than blind-copy the client's stale snapshot.
    """
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        payload = request.get_json(force=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Invalid payload"}), 400

        current = {}
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE) as f:
                current = json.load(f)

        force = str(request.args.get("force", "")).lower() in ("1", "true", "yes")
        problems = guard_destructive(current, payload, force)
        if problems:
            log_sync("data_push_blocked", {"problems": problems})
            print(f"🛑 Blocked destructive data.json push: {problems}")
            return jsonify({
                "error": "Destructive push blocked",
                "problems": problems,
                "hint": "Pull from Railway first, or re-send with ?force=1 to override.",
            }), 409

        def _record_ts(rec: dict):
            """Best available 'last modified' marker on a record, for
            deciding which side of a merge conflict is newer. Missing/
            unparseable timestamps sort as very old, so an established
            server-side record beats an untimestamped stray one by default
            — the safer direction when in doubt."""
            for key in ("updated_at", "posted_at", "created_at"):
                raw = rec.get(key)
                if raw:
                    try:
                        return datetime.fromisoformat(str(raw).replace("Z", ""))
                    except Exception:
                        continue
            return datetime.min

        def _merge_by_key(cur_map: dict, new_map: dict) -> dict:
            """Last-write-wins merge. Keys on only one side always survive —
            that's what stops a stale push from deleting a poll or a race
            entry the other side doesn't know about yet. Keys on BOTH sides
            (e.g. /fixrace corrected a race the app also has a copy of, or a
            poll closed server-side while the app's payload still carries
            it open) are resolved by timestamp, not by which side is doing
            the pushing — either side can be the legitimate correction."""
            out = dict(cur_map)
            for k, new_rec in new_map.items():
                if k not in out or _record_ts(new_rec) >= _record_ts(out[k]):
                    out[k] = new_rec
            return out

        merged = dict(current)
        merged.update(payload)   # scalar fields: client wins, as before

        # "polls" — list of dicts keyed by "id".
        cur_polls = {p.get("id"): p for p in current.get("polls", []) if p.get("id")}
        new_polls = {p.get("id"): p for p in payload.get("polls", []) if p.get("id")}
        merged["polls"] = list(_merge_by_key(cur_polls, new_polls).values())

        # "race_history" — dict keyed by "race_N".
        merged["race_history"] = _merge_by_key(
            current.get("race_history", {}) or {}, payload.get("race_history", {}) or {})

        if os.path.exists(DATA_FILE):
            backup_dir = os.path.join(_DATA_DIR, "backups")
            os.makedirs(backup_dir, exist_ok=True)
            ts = datetime.utcnow().strftime("%Y%m%d_%H%M%S_%f")
            shutil.copy2(DATA_FILE, os.path.join(backup_dir, f"data_{ts}.json"))
            backups = sorted([f for f in os.listdir(backup_dir) if f.startswith("data_")],
                             reverse=True)
            for old in backups[20:]:
                try:
                    os.remove(os.path.join(backup_dir, old))
                except Exception:
                    pass

        with open(DATA_FILE, "w") as f:
            json.dump(merged, f, indent=2)
        log_sync("data_push", {
            "standings": len(merged.get("standings", {}) or {}),
            "race_number": merged.get("race_number"),
            "polls": len(merged.get("polls", [])),
            "forced": force,
        })
        return jsonify({"status": "ok", "forced": force, "data": merged}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sync_app.route("/sync/registration", methods=["GET"])
def get_registration():
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    if not os.path.exists(REG_FILE):
        return jsonify({"drivers": [], "teams": [], "max_field": 40, "entry_fee": 20}), 200
    with open(REG_FILE) as f:
        return jsonify(json.load(f)), 200

@sync_app.route("/sync/registration", methods=["POST"])
def post_registration():
    """Accept a registration push from the desktop app — MERGED, never blind.

    The desktop app's copy is a snapshot from whenever it last pulled. Drivers
    who registered in Discord since then exist here but not in that snapshot.
    A straight overwrite silently deletes them, which is exactly how a 27-driver
    field became 21. So: the server is authoritative for driver EXISTENCE, the
    client is authoritative for admin FIELDS (paid, status, number, team).

    Teams get the identical treatment and for the identical reason — a team
    created via /createteam after the app's last pull isn't in its snapshot,
    and a blind overwrite deleted one mid-testing (JCOMM Motorsports vanished
    between two /teaminvite calls). Server is authoritative for team
    EXISTENCE, client wins on fields (points, looking, discord_role_id, member
    list) when it has the team at all.

    Deletions are still possible, but only when explicit — the client lists
    the driver key / team name in `removed_ids` / `removed_teams`.
    """
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        payload = request.get_json(force=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "Invalid payload"}), 400

        current = {}
        if os.path.exists(REG_FILE):
            with open(REG_FILE) as f:
                current = json.load(f)

        def key_of(d):
            return str(d.get("discord_id") or "").strip() or \
                   (d.get("name", "") or "").strip().lower()

        def team_key(t):
            return (t.get("name", "") or "").strip().lower()

        def merge_collection(cur_list, inc_list, keyfn, tombs):
            """Server is authoritative for EXISTENCE, client wins on fields.

            Records only vanish when explicitly tombstoned. Used for every
            record collection so a stale client snapshot can never delete
            anything — drivers were fixed this way first, then teams had to
            be fixed identically a day later, so this is now shared."""
            incoming = {keyfn(x): x for x in (inc_list or [])}
            out, seen = [], set()
            for x in (cur_list or []):
                k = keyfn(x)
                if k in tombs:
                    continue
                out.append(incoming.get(k, x))
                seen.add(k)
            for k, x in incoming.items():
                if k not in seen and k not in tombs:
                    out.append(x)
            return out

        tombstones      = {str(k).strip().lower() for k in payload.get("removed_ids", [])}
        team_tombstones = {str(k).strip().lower() for k in payload.get("removed_teams", [])}

        merged       = merge_collection(current.get("drivers"), payload.get("drivers"),
                                        key_of, tombstones)
        merged_teams = merge_collection(current.get("teams"), payload.get("teams"),
                                        team_key, team_tombstones)

        # Any OTHER list-of-records key gets the same protection automatically.
        # Without this, the next collection added to registration.json would
        # repeat this exact bug a third time.
        handled    = {"drivers", "teams", "removed_ids", "removed_teams"}
        extra_keys = {k for k, v in list(current.items()) + list(payload.items())
                      if k not in handled and isinstance(v, list)
                      and all(isinstance(i, dict) for i in (v or []))}
        extra_merged = {}
        for k in extra_keys:
            def generic_key(x):
                return str(x.get("id") or x.get("discord_id") or
                           x.get("name", "")).strip().lower()
            extra_merged[k] = merge_collection(current.get(k), payload.get(k),
                                               generic_key, set())

        result = dict(current)
        result.update({k: v for k, v in payload.items()
                       if k not in handled and k not in extra_keys})
        result["drivers"] = merged
        result["teams"]   = merged_teams
        result.update(extra_merged)
        result.pop("removed_ids", None)
        result.pop("removed_teams", None)

        # Rosters win over the cached `team` field. Without this, a stale
        # client record overwrites a driver who just joined a team in Discord.
        corrected = reconcile_driver_teams(result)

        dropped = len(current.get("drivers", [])) - len(
            [d for d in current.get("drivers", []) if key_of(d) not in tombstones])
        save_reg(result)
        log_sync("registration_push", {
            "drivers_before": len(current.get("drivers", []) or []),
            "drivers_after":  len(merged),
            "teams_before":   len(current.get("teams", []) or []),
            "teams_after":    len(merged_teams),
            "tombstoned_drivers": sorted(tombstones),
            "tombstoned_teams":   sorted(team_tombstones),
            "team_fields_corrected": corrected,
        })
        return jsonify({"status": "ok", "drivers": len(merged), "teams": len(merged_teams),
                        "removed": dropped}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@sync_app.route("/sync/betting", methods=["GET"])
def get_betting():
    """Read-only view for the desktop app's Sportsbook tab — just the three
    keys it needs instead of the whole data.json."""
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    data = load_data()
    return jsonify({
        "economy":     data.get("economy", {"balances": {}, "history": {}, "double_down_used": {}}),
        "bets":        data.get("bets", {}),
        "odds_board":  data.get("odds_board", {}),
        "race_number": data.get("race_number", 1),
    }), 200


@sync_app.route("/sync/betting/action", methods=["POST"])
def post_betting_action():
    """Admin control actions for Dale's Book, called from the desktop app's
    Sportsbook tab. Deliberately NOT a whole-file /sync/data push: balances
    and open bets change continuously from live /wager calls on Discord, so
    a push built from a possibly-stale local snapshot could silently
    clobber a bet placed seconds earlier. Every action here reads the
    CURRENT server-side data.json fresh, applies one targeted change, and
    saves — the same pattern the bot's own commands use.

    Body: {"action": "...", ...action-specific fields}
      adjust_balance  {discord_id, delta, reason}
      void_bet        {race_number, index}    — refunds stake, marks voided
      set_window      {open: true|false}
      settle          {race_number?}          — defaults to the open board's race
      rebuild_board   {}                      — regenerates data['odds_board']
      reset_economy   {}                      — every balance back to STARTING_BALANCE
    """
    if not check_token(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        payload = request.get_json(force=True) or {}
        action  = payload.get("action")
        data    = load_data()
        reg     = load_reg()

        if action == "adjust_balance":
            did    = str(payload.get("discord_id", "")).strip()
            delta  = int(payload.get("delta", 0))
            reason = payload.get("reason") or "Admin adjustment"
            if not did:
                return jsonify({"error": "discord_id required"}), 400
            ensure_balance(data, did)
            data["economy"]["balances"][did] += delta
            record_ledger(data, did, data.get("race_number", 1), delta, reason)
            save_data(data)
            return jsonify({"status": "ok", "balance": data["economy"]["balances"][did]}), 200

        elif action == "void_bet":
            race_key = str(payload.get("race_number", ""))
            idx      = payload.get("index")
            bets     = data.get("bets", {}).get(race_key, [])
            if idx is None or not isinstance(idx, int) or not (0 <= idx < len(bets)):
                return jsonify({"error": "bet not found"}), 404
            bet = bets[idx]
            if bet.get("settled"):
                return jsonify({"error": "already settled, can't void"}), 400
            did = bet["discord_id"]
            ensure_balance(data, did)
            data["economy"]["balances"][did] += bet["stake"]
            record_ledger(data, did, int(race_key) if race_key.isdigit() else 0,
                           bet["stake"], "Bet voided by admin (refund)")
            bet["settled"] = True
            bet["won"]     = None
            bet["voided"]  = True
            save_data(data)
            return jsonify({"status": "ok"}), 200

        elif action == "set_window":
            board = data.get("odds_board")
            if not board:
                return jsonify({"error": "no board posted yet"}), 400
            board["open"] = bool(payload.get("open", False))
            save_data(data)
            return jsonify({"status": "ok", "open": board["open"]}), 200

        elif action == "settle":
            race_num = payload.get("race_number") or (data.get("odds_board") or {}).get("race_number")
            if not race_num:
                return jsonify({"error": "no race to settle"}), 400
            changes = grade_and_settle_race(data, reg, int(race_num))
            save_data(data)
            return jsonify({"status": "ok", "settled": len(changes)}), 200

        elif action == "rebuild_board":
            board, problems = build_odds_board(data, reg)
            if problems:
                return jsonify({"error": "Board failed validation", "problems": problems}), 400
            save_data(data)
            return jsonify({"status": "ok", "board": board}), 200

        elif action == "reset_economy":
            econ = data.setdefault("economy", {"balances": {}, "history": {}, "double_down_used": {}})
            for did in list(econ.get("balances", {}).keys()):
                econ["balances"][did] = STARTING_BALANCE
            econ["double_down_used"] = {}
            save_data(data)
            return jsonify({"status": "ok"}), 200

        else:
            return jsonify({"error": f"unknown action '{action}'"}), 400

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def run_sync_server():
    """Run Flask in a daemon thread — dies when the bot dies."""
    sync_app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

# Start sync server in background before bot connects
_sync_thread = threading.Thread(target=run_sync_server, daemon=True)
_sync_thread.start()
print(f"✅  Sync server started on port {PORT}")

# ─────────────────────────────────────────────────────────────────
bot.run(BOT_TOKEN)