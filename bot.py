import discord
from discord.ext import commands, tasks
import json
import os
import csv
import io
import aiohttp
from datetime import datetime, timedelta
import asyncio

# ═══════════════════════════════════════════════════════════════════
#  CONFIG — Railway Environment Variables
#  Required: BOT_TOKEN, GUILD_ID, OWNER_ID, RACE_DAY, RACE_TIME_UTC
#  Optional: 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 (enables AI responses)
# ═══════════════════════════════════════════════════════════════════
BOT_TOKEN         = os.environ.get("MTUxMjE0ODEwMTE4NjU4NDczNg.G92HNE.NHbKXzO8p_QWuf_96wNOyqggyZ7mLIH6C2L60s")
GUILD_ID          = int(os.environ.get("963537794976845876", 0))
OWNER_ID          = int(os.environ.get("765193655916560414", 0))
RACE_DAY          = int(os.environ.get("RACE_DAY", 0))
RACE_TIME_UTC     = os.environ.get("RACE_TIME_UTC", "01:00")
ANTHROPIC_API_KEY = os.environ.get("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", "")

ANNOUNCEMENTS_CH  = "series-announcements"
ASK_DALE_CH       = "ask-dale"
# ═══════════════════════════════════════════════════════════════════

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

DATA_FILE = "data.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return {"standings": {}, "schedule": [], "race_number": 1}
    with open(DATA_FILE) as f:
        return json.load(f)


# ─────────────────────────────────────────────────────────────────
#  QSR KNOWLEDGE BASE
#  This is what Ask Dale knows about your specific league.
#  Update this as your league evolves.
# ─────────────────────────────────────────────────────────────────

QSR_KNOWLEDGE = """
You are Dale, the official AI assistant for QSR Simulations and the QSR Full Throttle Series.
You are an expert in iRacing, oval racing, NASCAR, and the QSR league specifically.
You are friendly, knowledgeable, and enthusiastic about sim racing.
Keep responses concise and Discord-friendly (no walls of text).
Use occasional racing emojis to keep things lively.

=== QSR FULL THROTTLE SERIES — LEAGUE FACTS ===

SERIES INFO:
- Car: ARCA Menards Series car at 110% horsepower (full power, no restriction)
- Race day: Every Monday at 8:00 PM Eastern Time
- Platform: iRacing — League Sessions feature
- Server: QSR Simulations Discord

POINTS SYSTEM:
- NASCAR Cup Series points format: 40 pts for win, 35 for 2nd, 34 for 3rd, down to 1 pt minimum
- Stage points awarded to top 10 at each stage end (10-9-8-7-6-5-4-3-2-1)
- IMPORTANT: Stages run GREEN FLAG — no caution is thrown at stage end
- No playoffs — full season points champion only
- Tiebreaker: most wins → most top 5s → most top 10s → best avg finish

RULES SUMMARY:
- Incident limit: 17x per race
- Intentional wrecking: immediate DQ, zero points
- Retaliation: treated same as intentional wrecking — use protest system instead
- Blocking: one defensive move per straightaway maximum
- Bump drafting: permitted on oval tracks
- Appeals: $1 deposit, refunded if appeal upheld, 24 hour window to appeal

REGISTRATION:
- Register in #registration channel on Discord
- Claim car number in #number-request (check #number-list first)
- Numbers are first-come first-served, locked for the season

PROTESTS:
- Submit in #penalty-report within 24 hours of race
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
- #number-list: Taken car numbers
- #number-request: Request your number
- #registration: Sign up for races
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
"""


# ─────────────────────────────────────────────────────────────────
#  CLAUDE AI — Ask Dale intelligence
# ─────────────────────────────────────────────────────────────────

async def ask_claude(question: str, context: str = "") -> str:
    """Send a question to Claude API and return the response."""
    if not ANTHROPIC_API_KEY:
        return None

    # Include current standings/schedule context if available
    data = load_data()
    standings = data.get("standings", {})
    schedule  = data.get("schedule", [])
    race_num  = data.get("race_number", 1)

    live_context = ""
    if standings:
        sorted_s = sorted(standings.items(), key=lambda x: x[1]["points"], reverse=True)
        top5 = ", ".join(f"{i+1}. {name} ({info['points']}pts)"
                         for i, (name, info) in enumerate(sorted_s[:5]))
        live_context += f"\nCURRENT STANDINGS TOP 5: {top5}"
        live_context += f"\nRACE NUMBER: {race_num - 1} races completed"

    if schedule:
        upcoming = [r for r in schedule if not r.get("complete")]
        if upcoming:
            live_context += f"\nNEXT RACE: {upcoming[0]['track']} on {upcoming[0]['date']}"

    system_prompt = QSR_KNOWLEDGE + live_context

    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": 500,
        "system": system_prompt,
        "messages": [
            {"role": "user", "content": question}
        ]
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


# ─────────────────────────────────────────────────────────────────
#  FALLBACK FAQ (used if no API key is set)
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
    "appeal":   "📝 Appeals cost $1 and must be filed within 24 hrs of the penalty decision. Your $1 is refunded if the appeal is upheld. Post in `#penalty-report` to begin.",
    "incident": "⚠️ Incident limit is 17x per race. First offense = warning. Second = points deduction. Third+ = Race Control discretion.",
    "blocking": "🚗 One defensive move per straightaway is allowed. Erratic or repeated blocking that causes contact is penalized.",
    "bump":     "💥 Bump drafting is permitted on oval tracks. Intentional spinning or wrecking via contact is NOT permitted.",
    "iracing":  "🎮 We race on iRacing using the League Sessions feature. Join under QSR Simulations to find our hosted sessions.",
    "arca":     "🏎️ The ARCA Menards car runs at 110% HP in our series — that means full unrestricted power. It's fast, it's loud, it's QSR Full Throttle.",
}


# ─────────────────────────────────────────────────────────────────
#  BOT EVENTS
# ─────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    print(f"✅  Ask Dale Bot online as {bot.user}")
    race_reminder.start()
    await bot.change_presence(activity=discord.Game("QSR Full Throttle Series 🏁"))
    if ANTHROPIC_API_KEY:
        print("✅  Claude AI enabled — Ask Dale is fully intelligent!")
    else:
        print("⚠️  No ANTHROPIC_API_KEY — using FAQ mode only")


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
#  RACE REMINDER
# ─────────────────────────────────────────────────────────────────

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
#  ASK DALE — Main Q&A command
# ─────────────────────────────────────────────────────────────────

@bot.command(name="ask")
async def ask(ctx, *, question: str = ""):
    if not question:
        await ctx.send(
            "❓ Ask me anything! Try:\n"
            "`!ask how do stage points work`\n"
            "`!ask what is bump drafting`\n"
            "`!ask how do I protest an incident`\n"
            "`!ask what is iRating`\n"
            "`!ask who won at Daytona in 2001`"
        )
        return

    # Show typing indicator while thinking
    async with ctx.typing():
        # Try Claude AI first
        if ANTHROPIC_API_KEY:
            response = await ask_claude(question)
            if response:
                embed = discord.Embed(
                    description=response,
                    color=0xE8272A
                )
                embed.set_footer(text="Ask Dale | QSR Full Throttle Series")
                await ctx.send(embed=embed)
                return

        # Fallback to FAQ if no API key or API failed
        q = question.lower()
        for key, answer in FAQ.items():
            if key in q:
                embed = discord.Embed(description=answer, color=0xE8272A)
                embed.set_footer(text="QSR Full Throttle | Ask Dale")
                await ctx.send(embed=embed)
                return

        await ctx.send(
            f"🤔 I don't have a specific answer for that. Try `#help-desk` or tag @Admin.\n"
            f"Or ask me something else — I know a lot about iRacing and oval racing!"
        )


@bot.command(name="dale")
async def dale(ctx, *, question: str = ""):
    """Alternative to !ask — just tag Dale directly."""
    await ask(ctx, question=question)


# ─────────────────────────────────────────────────────────────────
#  MEMBER COMMANDS
# ─────────────────────────────────────────────────────────────────

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
    embed.set_footer(text=f"Through Race {data.get('race_number',1)-1} | Updated after each race by Race Control Bot")
    await ctx.send(embed=embed)

@bot.command(name="schedule")
async def schedule_cmd(ctx):
    data  = load_data()
    sched = data.get("schedule", [])
    if not sched:
        await ctx.send("📅 Schedule not loaded yet. Check back soon!")
        return
    embed = discord.Embed(title="📅 QSR Full Throttle — Season Schedule", color=0xE8272A)
    lines = []
    for i, race in enumerate(sched, 1):
        done = "✅" if race.get("complete") else "🔜"
        lines.append(f"{done} **Race {i}** — {race['track']} | {race['date']}")
    embed.description = "\n".join(lines)
    await ctx.send(embed=embed)

@bot.command(name="rules")
async def rules_cmd(ctx):
    embed = discord.Embed(
        title="📋 QSR Full Throttle Series — Quick Rules",
        color=0xE8272A
    )
    embed.add_field(name="Car",                  value="ARCA Menards @ 110% HP", inline=True)
    embed.add_field(name="Race Day",             value="Mondays 8PM ET", inline=True)
    embed.add_field(name="Points",               value="NASCAR system (40 pts win)", inline=True)
    embed.add_field(name="Stages",               value="Green flag only — no caution", inline=True)
    embed.add_field(name="Incident Limit",       value="17x per race", inline=True)
    embed.add_field(name="Intentional Wrecking", value="Immediate DQ", inline=True)
    embed.add_field(name="Appeals",              value="$1 deposit, refunded if upheld", inline=True)
    embed.add_field(name="Full Rulebook",        value="See `#league-rules`", inline=True)
    embed.set_footer(text="Use !ask <question> for more detail on anything")
    await ctx.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  ADMIN COMMANDS
# ─────────────────────────────────────────────────────────────────

def is_owner():
    async def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

@bot.command(name="loadschedule")
@is_owner()
async def load_schedule(ctx):
    """Attach a CSV with columns: Track,Date"""
    if not ctx.message.attachments:
        await ctx.send("📎 Attach a CSV with columns: `Track,Date`")
        return
    raw    = await ctx.message.attachments[0].read()
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8")))
    data   = load_data()
    data["schedule"] = [{"track": r["Track"], "date": r["Date"], "complete": False} for r in reader]
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
    await ctx.send(f"✅ Schedule loaded — {len(data['schedule'])} races.")

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
#  HELP & ERRORS
# ─────────────────────────────────────────────────────────────────

@bot.command(name="help")
async def help_cmd(ctx):
    ai_status = "✅ AI Enabled" if ANTHROPIC_API_KEY else "⚠️ FAQ Mode"
    embed = discord.Embed(
        title=f"🤖 Ask Dale Bot — Commands [{ai_status}]",
        color=0xE8272A
    )
    embed.add_field(
        name="!ask <anything>",
        value="Ask Dale anything — rules, iRacing, NASCAR history, racing tips, standings, and more",
        inline=False
    )
    embed.add_field(name="!dale <question>", value="Same as !ask", inline=False)
    embed.add_field(name="!standings",       value="Current championship standings", inline=False)
    embed.add_field(name="!schedule",        value="Season race schedule", inline=False)
    embed.add_field(name="!rules",           value="Quick rules summary", inline=False)
    embed.add_field(name="── Admin ──",      value="\u200b", inline=False)
    embed.add_field(name="!loadschedule",    value="Load schedule from CSV (Track,Date)", inline=False)
    embed.add_field(name="!restructure",     value="Rebuild Discord channel layout", inline=False)
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
