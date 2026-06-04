import discord
from discord.ext import commands, tasks
import json
import os
import hashlib
import base64
import aiohttp
import asyncio
from datetime import datetime, timedelta
import csv
import io

# ═══════════════════════════════════════════════════════════════════
#  CONFIG — all values loaded from Railway Environment Variables
# ═══════════════════════════════════════════════════════════════════
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
GUILD_ID          = int(os.environ.get("GUILD_ID", 0))
IRACING_EMAIL     = os.environ.get("IRACING_EMAIL")
IRACING_PASSWORD  = os.environ.get("IRACING_PASSWORD")
IRACING_LEAGUE_ID = int(os.environ.get("IRACING_LEAGUE_ID", 0))
OWNER_ID          = int(os.environ.get("OWNER_ID", 0))
RACE_DAY          = int(os.environ.get("RACE_DAY", 0))        # 0=Mon
RACE_TIME_UTC     = os.environ.get("RACE_TIME_UTC", "01:00")  # 8PM ET = 01:00 UTC
RACE_DURATION_HRS = int(os.environ.get("RACE_DURATION_HRS", 2))

STANDINGS_CH      = "points-standings"
ANNOUNCEMENTS_CH  = "series-announcements"
RESULTS_CH        = "race-results"
ASK_DALE_CH       = "ask-dale"
# ═══════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

DATA_FILE   = "data.json"
COOKIE_FILE = "iracing_cookies.json"

# NASCAR points — positions 1 through 43
NASCAR_POINTS = [
    40,35,34,33,32,31,30,29,28,27,
    26,25,24,23,22,21,20,19,18,17,
    16,15,14,13,12,11,10, 9, 8, 7,
     6, 5, 4, 3, 2, 1, 1, 1, 1, 1,
     1, 1, 1
]
STAGE_POINTS = [10,9,8,7,6,5,4,3,2,1]  # top 10, no caution


# ─────────────────────────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────────────────────────

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"standings": {}, "schedule": [], "race_number": 1,
                "last_session_id": None}
    with open(DATA_FILE) as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ─────────────────────────────────────────────────────────────────
#  iRACING API
# ─────────────────────────────────────────────────────────────────

IRACING_BASE = "https://members-ng.iracing.com"

async def iracing_login(session: aiohttp.ClientSession) -> bool:
    """Hash password per iRacing spec and log in."""
    email   = IRACING_EMAIL.lower().strip()
    pw_hash = base64.b64encode(
        hashlib.sha256(
            (IRACING_PASSWORD + email).encode("utf-8")
        ).digest()
    ).decode("utf-8")

    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://members.iracing.com",
        "Referer": "https://members.iracing.com/",
    }

    for url in [
        "https://members-ng.iracing.com/auth",
        "https://members.iracing.com/membersite/login.jsp",
    ]:
        try:
            resp = await session.post(
                url,
                json={"email": email, "password": pw_hash},
                headers=headers,
                allow_redirects=True,
                timeout=aiohttp.ClientTimeout(total=15)
            )
            print(f"iRacing login attempt at {url}: HTTP {resp.status}")
            if resp.status == 200:
                try:
                    data = await resp.json(content_type=None)
                    if data.get("authcode", 0) != 0:
                        cookies = {c.key: c.value for c in session.cookie_jar}
                        with open(COOKIE_FILE, "w") as f:
                            json.dump(cookies, f)
                        print("✅ iRacing login successful")
                        return True
                    else:
                        print(f"❌ iRacing rejected login: authcode=0, message={data.get('message','unknown')}")
                        return False
                except Exception as e:
                    print(f"Login response parse error: {e}")
                    continue
        except Exception as e:
            print(f"Login request error at {url}: {e}")
            continue

    print("❌ iRacing login failed — all endpoints exhausted")
    return False


async def iracing_get(session: aiohttp.ClientSession, path: str) -> dict | None:
    """GET a members-ng endpoint, auto-following iRacing's link pattern."""
    resp = await session.get(f"{IRACING_BASE}{path}")
    if resp.status == 401:
        if await iracing_login(session):
            resp = await session.get(f"{IRACING_BASE}{path}")
        else:
            return None
    if resp.status != 200:
        print(f"iRacing API error {resp.status} for {path}")
        return None
    data = await resp.json()
    if "link" in data:
        r2 = await session.get(data["link"])
        return await r2.json()
    return data


async def get_latest_league_session(session: aiohttp.ClientSession) -> dict | None:
    """Return the most recent completed session for our league."""
    data = await iracing_get(session, f"/data/league/seasons?league_id={IRACING_LEAGUE_ID}&include_licenses=false")
    if not data:
        return None
    seasons = data.get("seasons", [])
    if not seasons:
        return None
    season    = seasons[0]
    season_id = season["season_id"]
    sessions_data = await iracing_get(
        session,
        f"/data/league/season_sessions?league_id={IRACING_LEAGUE_ID}&season_id={season_id}&results_only=true"
    )
    if not sessions_data:
        return None
    sessions = sessions_data.get("sessions", [])
    if not sessions:
        return None
    return sorted(sessions, key=lambda s: s.get("launch_at", ""), reverse=True)[0]


async def get_session_results(session: aiohttp.ClientSession, subsession_id: int) -> dict | None:
    """Pull full results for a completed subsession."""
    data = await iracing_get(session, f"/data/results/get?subsession_id={subsession_id}")
    if not data:
        return None
    return data


# ─────────────────────────────────────────────────────────────────
#  POINTS CALCULATOR
# ─────────────────────────────────────────────────────────────────

def calculate_points(results_data: dict) -> list:
    session_results = results_data.get("session_results", [])
    race_session = None
    for s in session_results:
        if s.get("simsession_type_name") == "Race":
            race_session = s
            break
    if not race_session:
        return []
    results = []
    for result in race_session.get("results", []):
        pos       = result.get("finish_position", 99) + 1
        name      = result.get("display_name", "Unknown")
        cust_id   = result.get("cust_id", 0)
        incidents = result.get("incidents", 0)
        race_pts  = NASCAR_POINTS[pos - 1] if pos <= len(NASCAR_POINTS) else 1
        results.append((pos, name, cust_id, race_pts, incidents))
    return sorted(results, key=lambda x: x[0])


# ─────────────────────────────────────────────────────────────────
#  AUTO-POST RESULTS
# ─────────────────────────────────────────────────────────────────

async def post_race_results(subsession_id: int, stage_pts_map: dict = None):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    async with aiohttp.ClientSession() as session:
        await iracing_login(session)
        results_data = await get_session_results(session, subsession_id)

    if not results_data:
        print("Could not fetch session results.")
        return

    results = calculate_points(results_data)
    if not results:
        print("No race results found in session data.")
        return

    stage_pts_map = stage_pts_map or {}
    data          = load_data()
    race_num      = data.get("race_number", 1)

    for pos, name, cust_id, race_pts, incidents in results:
        stage = stage_pts_map.get(str(cust_id), 0)
        total = race_pts + stage
        if name not in data["standings"]:
            data["standings"][name] = {"points": 0, "wins": 0, "races": 0, "incidents": 0}
        data["standings"][name]["points"]    += total
        data["standings"][name]["races"]     += 1
        data["standings"][name]["incidents"] += incidents
        if pos == 1:
            data["standings"][name]["wins"] += 1

    data["race_number"]     = race_num + 1
    data["last_session_id"] = subsession_id
    save_data(data)

    medals = {1:"🏆", 2:"🥈", 3:"🥉"}

    # Results embed
    results_embed = discord.Embed(
        title=f"🏁 Race {race_num} Official Results — QSR Full Throttle Series",
        color=0xE8272A,
        timestamp=datetime.utcnow()
    )
    lines = []
    for pos, name, cust_id, race_pts, incidents in results[:20]:
        icon      = medals.get(pos, f"`{pos:>2}.`")
        stage     = stage_pts_map.get(str(cust_id), 0)
        total     = race_pts + stage
        inc_str   = f" ⚠️{incidents}x" if incidents else ""
        stage_str = f" +{stage} stage" if stage else ""
        lines.append(f"{icon} **{name}** — {race_pts}{stage_str} = **{total} pts**{inc_str}")
    results_embed.description = "\n".join(lines)
    results_embed.set_footer(text="Stage points run green flag — no caution | Use !standings for full table")

    # Standings embed
    sorted_s = sorted(data["standings"].items(), key=lambda x: x[1]["points"], reverse=True)
    standings_embed = discord.Embed(
        title="🏆 Updated Championship Standings",
        color=0xFFD700,
        timestamp=datetime.utcnow()
    )
    s_lines = []
    for i, (driver, info) in enumerate(sorted_s[:20], 1):
        icon    = medals.get(i, f"`{i:>2}.`")
        wins    = info.get("wins", 0)
        win_str = f" ⭐x{wins}" if wins else ""
        s_lines.append(f"{icon} **{driver}** — {info['points']} pts{win_str}")
    standings_embed.description = "\n".join(s_lines)
    standings_embed.set_footer(text=f"Through Race {race_num}")

    results_ch   = discord.utils.get(guild.text_channels, name=RESULTS_CH)
    standings_ch = discord.utils.get(guild.text_channels, name=STANDINGS_CH)
    if results_ch:
        await results_ch.send(embed=results_embed)
    if standings_ch:
        await standings_ch.send(embed=standings_embed)

    print(f"✅ Race {race_num} results posted automatically.")


# ─────────────────────────────────────────────────────────────────
#  SCHEDULED TASKS
# ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=5)
async def check_race_results():
    now        = datetime.utcnow()
    race_hour, race_min = map(int, RACE_TIME_UTC.split(":"))
    race_start = now.replace(hour=race_hour, minute=race_min, second=0, microsecond=0)
    race_end   = race_start + timedelta(hours=RACE_DURATION_HRS)
    window_end = race_end   + timedelta(minutes=30)

    if now.weekday() != RACE_DAY:
        return
    if not (race_end <= now <= window_end):
        return

    data    = load_data()
    last_id = data.get("last_session_id")

    async with aiohttp.ClientSession() as session:
        await iracing_login(session)
        latest = await get_latest_league_session(session)

    if not latest:
        return

    subsession_id = latest.get("subsession_id") or latest.get("sessions", [{}])[0].get("subsession_id")
    if not subsession_id or subsession_id == last_id:
        return

    print(f"🏁 New session detected: {subsession_id} — posting results...")
    await post_race_results(subsession_id)


@tasks.loop(hours=1)
async def race_reminder():
    now = datetime.utcnow()
    if now.weekday() != RACE_DAY:
        return
    race_hour, race_min = map(int, RACE_TIME_UTC.split(":"))
    race_time    = now.replace(hour=race_hour, minute=race_min, second=0, microsecond=0)
    one_hour_out = race_time - timedelta(hours=1)
    if abs((now - one_hour_out).total_seconds()) < 3600:
        guild = bot.get_guild(GUILD_ID)
        if not guild:
            return
        ch = discord.utils.get(guild.text_channels, name=ANNOUNCEMENTS_CH)
        if ch:
            embed = discord.Embed(
                title="🏁 RACE NIGHT — 1 Hour Out!",
                description=(
                    "Green flag in **60 minutes**!\n\n"
                    "✅ Lock in your setup\n"
                    "✅ Join the hosted league session\n"
                    "✅ Check `#how-to-watch` for the stream link\n\n"
                    "@everyone Let's go racing! 🔥"
                ),
                color=0xE8272A
            )
            await ch.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  BOT EVENTS
# ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅  QSR Full Throttle Bot online as {bot.user}")
    check_race_results.start()
    race_reminder.start()
    await bot.change_presence(activity=discord.Game("QSR Full Throttle Series 🏁"))


@bot.event
async def on_member_join(member: discord.Member):
    ch = discord.utils.get(member.guild.text_channels, name="welcome")
    if ch:
        embed = discord.Embed(
            title="🏁  Welcome to QSR Simulations!",
            description=(
                f"Hey {member.mention}, glad you're here!\n\n"
                "**QSR Full Throttle Series** — iRacing oval league running the "
                "ARCA Menards car at full 110% HP. Real power, real racing.\n\n"
                "**Get started in 3 steps:**\n"
                "1️⃣  Read the rules → `#league-rules`\n"
                "2️⃣  Claim your number → `#number-request`\n"
                "3️⃣  Register for the next race → `#registration`\n\n"
                "Questions? Ask in `#ask-dale` anytime. See you on track! 🔥"
            ),
            color=0xE8272A
        )
        embed.set_footer(text="QSR Simulations | Full Throttle Series")
        await ch.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  MEMBER COMMANDS
# ─────────────────────────────────────────────────────────────────

FAQ = {
    "rules":    "📋 Full rulebook in `#league-rules`. Bump-drafting allowed. Intentional wrecking = immediate DQ. All incidents reviewed within 48 hrs.",
    "schedule": "📅 Check `#schedule` for the full season calendar. Races every Monday at 8PM ET. Type `!schedule` for a quick list.",
    "points":   "🏆 NASCAR points system — 40 pts for the win, scaled to 1 pt minimum. Stage points (top 10, 10-1 pts) run **green flag, no caution**. Type `!standings` for current standings.",
    "car":      "🚗 ARCA Menards car at **110% horsepower**. No setup restrictions — bring your best.",
    "stages":   "🏁 Stages award top-10 finishers 10 down to 1 pt but **do NOT throw a caution**. Racing stays green. This is a defining rule of the QSR Full Throttle Series.",
    "register": "✍️ Head to `#registration` and follow the pinned post to sign up for the next race.",
    "number":   "🔢 Check `#number-list` for taken numbers, then post your request in `#number-request`. Numbers are first-come, first-served.",
    "protest":  "⚖️ Post in `#penalty-report` with your iRacing subsession ID and incident timestamp. Race Control reviews within 48 hrs. Appeals cost $1 — refunded if upheld.",
    "stream":   "📺 Check `#how-to-watch` for broadcast info. Stream details posted before each race.",
    "contact":  "📨 Tag an @Admin or post in `#help-desk` for direct staff help.",
}

@bot.command(name="ask")
async def ask(ctx, *, question: str = ""):
    if not question:
        keys = ", ".join(f"`{k}`" for k in FAQ)
        await ctx.send(f"❓ Try `!ask <topic>`. Topics: {keys}")
        return
    q = question.lower()
    for key, answer in FAQ.items():
        if key in q:
            embed = discord.Embed(description=answer, color=0xE8272A)
            embed.set_footer(text="QSR Full Throttle | Ask Dale Bot")
            await ctx.send(embed=embed)
            return
    await ctx.send(
        f"🤔 No answer found for **{question}**. Try `#help-desk` or tag @Admin.\n"
        f"Available topics: {', '.join(FAQ.keys())}"
    )

@bot.command(name="standings")
async def standings(ctx):
    data = load_data()
    s    = data.get("standings", {})
    if not s:
        await ctx.send("No standings yet — Race 1 incoming! 🏁")
        return
    sorted_s = sorted(s.items(), key=lambda x: x[1]["points"], reverse=True)
    embed    = discord.Embed(
        title="🏆 QSR Full Throttle Series — Championship Standings",
        color=0xE8272A,
        timestamp=datetime.utcnow()
    )
    medals = {1:"🥇", 2:"🥈", 3:"🥉"}
    lines  = []
    for i, (driver, info) in enumerate(sorted_s[:20], 1):
        icon    = medals.get(i, f"`{i:>2}.`")
        wins    = info.get("wins", 0)
        win_str = f" ⭐x{wins}" if wins else ""
        lines.append(f"{icon} **{driver}** — {info['points']} pts{win_str}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=f"Through Race {data.get('race_number',1)-1} | Auto-updated after each race")
    await ctx.send(embed=embed)

@bot.command(name="schedule")
async def schedule_cmd(ctx):
    data  = load_data()
    sched = data.get("schedule", [])
    if not sched:
        await ctx.send("📅 Schedule not loaded yet.")
        return
    embed = discord.Embed(title="📅 QSR Full Throttle — Season Schedule", color=0xE8272A)
    lines = []
    for i, race in enumerate(sched, 1):
        done = "✅" if race.get("complete") else "🔜"
        lines.append(f"{done} **Race {i}** — {race['track']} | {race['date']}")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  ADMIN COMMANDS  (owner only — locked to your Discord User ID)
# ─────────────────────────────────────────────────────────────────

def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

@bot.command(name="forceresults")
@is_owner()
async def force_results(ctx, subsession_id: int = 0):
    if subsession_id == 0:
        await ctx.send("Usage: `!forceresults <subsession_id>`\nFind the subsession ID in the iRacing results URL.")
        return
    await ctx.send(f"⏳ Fetching results for session `{subsession_id}`...")
    await post_race_results(subsession_id)

@bot.command(name="stagestage")
@is_owner()
async def add_stage_points(ctx, subsession_id: int, *, entries: str):
    stage_pts_map = {}
    for entry in entries.split():
        cid, pts = entry.split(":")
        stage_pts_map[cid.strip()] = int(pts.strip())
    await ctx.send("⏳ Re-processing race with stage points...")
    await post_race_results(subsession_id, stage_pts_map)

@bot.command(name="loadschedule")
@is_owner()
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

@bot.command(name="teststatus")
@is_owner()
async def test_status(ctx):
    await ctx.send("⏳ Testing iRacing API connection...")
    async with aiohttp.ClientSession() as session:
        ok = await iracing_login(session)
        if ok:
            latest = await get_latest_league_session(session)
            if latest:
                sid = latest.get("subsession_id", "N/A")
                await ctx.send(f"✅ iRacing API connected!\nLatest session ID: `{sid}`")
            else:
                await ctx.send("✅ Login OK but no league sessions found. Double-check your `IRACING_LEAGUE_ID` in Railway Variables.")
        else:
            await ctx.send("❌ iRacing login failed. Check Railway logs for the exact error.")

@bot.command(name="restructure")
@is_owner()
async def restructure(ctx):
    NEW_STRUCTURE = {
        "📋 FRONT DESK": ["welcome","get-roles","announcements","qsr-record-book"],
        "🏁 QSR FULL THROTTLE SERIES": [
            "series-announcements","schedule","points-standings","race-results",
            "league-rules","penalty-report","number-list","number-request","registration"
        ],
        "💬 COMMUNITY": [
            "pitlane","ask-dale","media-share","racing-irl",
            "meme-central","hot-takes","nascar-fan-chat","qsr-race-polls"
        ],
        "📺 BROADCAST & EVENTS": [
            "how-to-watch","hosted-sessions","qsr-live","league-socials","team-forming"
        ],
        "🔒 STAFF ONLY": ["staff-chat","staff-docs"],
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


# ─────────────────────────────────────────────────────────────────
#  HELP & ERROR HANDLING
# ─────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_cmd(ctx):
    embed = discord.Embed(title="🤖 QSR Bot Commands", color=0xE8272A)
    embed.add_field(name="!ask <topic>",    value="rules, schedule, points, car, stages, register, number, protest, stream, contact", inline=False)
    embed.add_field(name="!standings",      value="Current championship standings", inline=False)
    embed.add_field(name="!schedule",       value="Season race schedule", inline=False)
    embed.add_field(name="── Admin ──",     value="\u200b", inline=False)
    embed.add_field(name="!teststatus",     value="Test iRacing API connection", inline=False)
    embed.add_field(name="!forceresults <subsession_id>", value="Manually post results for a race", inline=False)
    embed.add_field(name="!stagestage <id> CustID:pts ...", value="Add stage points to a posted race", inline=False)
    embed.add_field(name="!loadschedule",   value="Load schedule from CSV (Track,Date)", inline=False)
    embed.add_field(name="!restructure",    value="Rebuild Discord channel layout", inline=False)
    await ctx.send(embed=embed)

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 You don't have permission to use that command.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"⚠️ Error: {error}")


# ─────────────────────────────────────────────────────────────────
bot.run(BOT_TOKEN)
