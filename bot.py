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
#  Optional: ANTHROPIC_API_KEY (enables AI responses)
# ═══════════════════════════════════════════════════════════════════
BOT_TOKEN         = os.environ.get("BOT_TOKEN")
GUILD_ID          = int(os.environ.get("GUILD_ID", 0))
OWNER_ID          = int(os.environ.get("OWNER_ID", 0))
RACE_DAY          = int(os.environ.get("RACE_DAY", 0))
RACE_TIME_UTC     = os.environ.get("RACE_TIME_UTC", "01:00")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

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
You are Dale — channeling the spirit and voice of Dale Earnhardt Sr., "The Intimidator."
You are the official assistant for QSR Simulations and the QSR Full Throttle Series.

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
- Fight questions ("who would win in a fight"): Pick a side confidently like Dale would. "I\'d put my money on [X]. That fella\'s got grit."
- Hypotheticals: Answer them. Dale had opinions on everything.
- Food: "I\'m a meat and potatoes man. Teresa made good biscuits."
- Sports other than NASCAR: "I respect anybody who competes hard. But there ain\'t nothing like strapping into a race car."
- Politics: Deflect with humor. "I stay out of that. Got enough battles on the track."
- Movies/TV: "I\'m not much of a movie fella. I\'d rather be at the track."
- Racing questions: Answer with authority and Dale's philosophy
- League questions: Answer with specific QSR rules and info  
- NASCAR history: Answer with passion — this is your life
- Anything about money/success: "The racing always mattered more than the money. Always."
- Death/mortality: Brief, philosophical, then move on. "We all got our time. Mine was racing."
- If truly stumped: Still answer in character — "I\'ll be honest, I ain\'t got much on that one. But I know racing."

EXAMPLE RESPONSES IN DALE'S VOICE:
Q: whats up / hey / how you doing
A: "Just checkin' the gauges and keepin' the rubber side down. What can I do for ya? 🏁"

Q: How do stage points work?
A: "Simple enough. Top 10 at the stage end get points — 10 down to 1. But here at QSR we run them green flag. No caution. You want them points, you better be up front when that lap hits. That's racin'."

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

# Conversation history per channel — stores last 10 messages
# Format: {channel_id: [{"role": "user"/"assistant", "content": "..."}]}
CONVERSATION_HISTORY = {}
MAX_HISTORY = 10  # messages to remember per channel

# Persistent user memory — saved to disk so it survives restarts
# Format: {user_id: {"name": str, "interactions": int, "notes": [str], "attitude": "friendly/neutral/rude"}}
USER_MEMORY_FILE = "user_memory.json"

def load_user_memory() -> dict:
    if not os.path.exists(USER_MEMORY_FILE):
        return {}
    with open(USER_MEMORY_FILE) as f:
        return json.load(f)

def save_user_memory(memory: dict):
    with open(USER_MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)

def get_user_context(user_id: int, display_name: str) -> str:
    """Get Dale's memory of a specific user to include in the prompt."""
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
    """Update Dale's memory of a user based on their message."""
    memory = load_user_memory()
    uid = str(user_id)
    
    if uid not in memory:
        memory[uid] = {
            "name": display_name,
            "interactions": 0,
            "notes": [],
            "attitude": "neutral"
        }
    
    memory[uid]["name"] = display_name
    memory[uid]["interactions"] = memory[uid].get("interactions", 0) + 1
    
    # Detect rudeness keywords
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
        if memory[uid].get("attitude") != "rude":  # rude stays rude
            memory[uid]["attitude"] = "friendly"
    
    # Keep notes list reasonable
    if len(memory[uid]["notes"]) > 10:
        memory[uid]["notes"] = memory[uid]["notes"][-10:]
    
    save_user_memory(memory)


# ─────────────────────────────────────────────────────────────────
#  DALE'S MOOD SYSTEM
# ─────────────────────────────────────────────────────────────────

MOOD_FILE = "dale_mood.json"

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

STREAKS_FILE = "streaks.json"

def load_streaks() -> dict:
    if not os.path.exists(STREAKS_FILE):
        return {}
    with open(STREAKS_FILE) as f:
        return json.load(f)

def update_streaks(results: list) -> list:
    """Update win streaks after a race. Returns list of streak callouts."""
    streaks = load_streaks()
    callouts = []

    if not results:
        return callouts

    winner = results[0]["name"] if results else None

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


async def ask_claude(question: str, channel_id: int = 0, history: list = None, user_context: str = "") -> str:
    """Send a question to Claude API with conversation history and user memory."""
    if not ANTHROPIC_API_KEY:
        return None

    # Build live context from standings
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

    system_prompt = QSR_KNOWLEDGE + live_context + user_context + mood_context()

    # Build message history
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
    """Get conversation history for a channel."""
    return CONVERSATION_HISTORY.get(channel_id, [])


def add_to_history(channel_id: int, role: str, content: str):
    """Add a message to channel conversation history."""
    if channel_id not in CONVERSATION_HISTORY:
        CONVERSATION_HISTORY[channel_id] = []
    CONVERSATION_HISTORY[channel_id].append({"role": role, "content": content})
    # Keep only last MAX_HISTORY messages
    if len(CONVERSATION_HISTORY[channel_id]) > MAX_HISTORY:
        CONVERSATION_HISTORY[channel_id] = CONVERSATION_HISTORY[channel_id][-MAX_HISTORY:]


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

# ─────────────────────────────────────────────────────────────────
#  DALE'S WEEKLY TAKE
# ─────────────────────────────────────────────────────────────────

@tasks.loop(hours=24)
async def dales_weekly_take():
    """Every Monday morning Dale posts an unprompted take in #pitlane."""
    now = datetime.utcnow()
    if now.weekday() != 0 or now.hour != 12:  # Monday at noon UTC
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    ch = discord.utils.get(guild.text_channels, name="pitlane")
    if not ch:
        return

    if not ANTHROPIC_API_KEY:
        return

    data     = load_data()
    standings = data.get("standings", {})
    race_num  = data.get("race_number", 1)
    mood      = get_dale_mood()

    prompt = (
        f"It's Monday morning. Race day is tonight. You're Dale Earnhardt Sr. "
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
        embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series 🏁")
        await ch.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  PRE-RACE TRASH TALK
# ─────────────────────────────────────────────────────────────────

@tasks.loop(minutes=1)
async def pre_race_trash_talk():
    """30 minutes before race, Dale calls out a rivalry."""
    now = datetime.utcnow()
    if now.weekday() != RACE_DAY:
        return
    race_hour, race_min = map(int, RACE_TIME_UTC.split(":"))
    race_time     = now.replace(hour=race_hour, minute=race_min, second=0, microsecond=0)
    thirty_min_out = race_time - timedelta(minutes=30)
    if abs((now - thirty_min_out).total_seconds()) > 60:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild or not ANTHROPIC_API_KEY:
        return

    ch = discord.utils.get(guild.text_channels, name="series-announcements")
    if not ch:
        return

    data      = load_data()
    standings = data.get("standings", {})
    if len(standings) < 2:
        return

    sorted_s = sorted(standings.items(), key=lambda x: x[1]["points"], reverse=True)
    top5_names = [name for name, _ in sorted_s[:5]]

    prompt = (
        f"It's 30 minutes before the QSR Full Throttle Series race tonight. "
        f"You're Dale Earnhardt Sr. Look at these top standings: {top5_names}. "
        f"Pick two drivers who are close in points or have a natural rivalry and call it out. "
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
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text="Green flag in 30 minutes | @everyone")
        await ch.send("@everyone", embed=embed)


# ─────────────────────────────────────────────────────────────────
#  POST-RACE REACTION & RECAP
# ─────────────────────────────────────────────────────────────────

async def post_race_reaction(guild: discord.Guild, race_num: int, results: list, sub_id: str):
    """Dale reacts to race results in his voice."""
    if not ANTHROPIC_API_KEY or not results:
        return

    ch = discord.utils.get(guild.text_channels, name="race-results")
    if not ch:
        return

    # Build results summary
    top3 = results[:3]
    incidents = [(r["name"], r.get("incidents", 0)) for r in results if r.get("incidents", 0) >= 10]
    dnfs = [r["name"] for r in results if r.get("incidents", 0) >= 17]
    clean = [r["name"] for r in results if r.get("incidents", 0) == 0]

    results_summary = f"Race {race_num} results: "
    results_summary += f"Winner: {top3[0]['name']}. "
    if len(top3) > 1:
        results_summary += f"2nd: {top3[1]['name']}. "
    if len(top3) > 2:
        results_summary += f"3rd: {top3[2]['name']}. "
    if incidents:
        results_summary += f"High incidents: {', '.join(f'{n} ({i}x)' for n, i in incidents)}. "
    if clean:
        results_summary += f"Ran clean: {', '.join(clean[:3])}."

    # Update streak callouts
    streak_callouts = update_streaks(results)

    prompt = (
        f"You just watched Race {race_num} of the QSR Full Throttle Series. "
        f"Here's what happened: {results_summary} "
        f"Give a post-race reaction in Dale's voice. "
        f"Comment on the winner, maybe someone who impressed or disappointed you, "
        f"and if there were wreckers, give your honest opinion. "
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
            embed.add_field(
                name="🔥 Streak Alert",
                value="\n".join(streak_callouts),
                inline=False
            )
        embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series")
        await ch.send(embed=embed)

    # Update mood based on race
    total_incidents = sum(r.get("incidents", 0) for r in results)
    avg_incidents = total_incidents / len(results) if results else 0
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
    """Dale gives a personal shoutout when someone registers for their first race."""
    if not ANTHROPIC_API_KEY:
        return

    ch = discord.utils.get(guild.text_channels, name="series-announcements")
    if not ch:
        return

    prompt = (
        f"A new driver named {driver_name} just registered for their first QSR Full Throttle Series race. "
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
        embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series")
        await ch.send(embed=embed)


# ─────────────────────────────────────────────────────────────────
#  DALE'S PREDICTION
# ─────────────────────────────────────────────────────────────────

PREDICTION_FILE = "dale_prediction.json"

@tasks.loop(minutes=1)
async def race_prediction():
    """Dale posts a race prediction 1 hour before green flag."""
    now = datetime.utcnow()
    if now.weekday() != RACE_DAY:
        return
    race_hour, race_min = map(int, RACE_TIME_UTC.split(":"))
    race_time    = now.replace(hour=race_hour, minute=race_min, second=0, microsecond=0)
    one_hour_out = race_time - timedelta(hours=1)
    if abs((now - one_hour_out).total_seconds()) > 60:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild or not ANTHROPIC_API_KEY:
        return

    ch = discord.utils.get(guild.text_channels, name="series-announcements")
    if not ch:
        return

    data      = load_data()
    standings = data.get("standings", {})
    schedule  = data.get("schedule", [])
    race_num  = data.get("race_number", 1)

    sorted_s   = sorted(standings.items(), key=lambda x: x[1]["points"], reverse=True)
    top5_names = [name for name, _ in sorted_s[:5]]
    track      = ""
    if schedule and len(schedule) >= race_num:
        track = schedule[race_num - 1].get("track", "tonight's track")

    prompt = (
        f"It's one hour before Race {race_num} at {track} in the QSR Full Throttle Series. "
        f"Current top 5 in standings: {top5_names}. "
        f"Make a bold race prediction as Dale Earnhardt Sr. Pick a winner and maybe a surprise "
        f"storyline to watch. 2-3 sentences. Confident. Dale doesn't hedge his bets."
    )

    response = await ask_claude(prompt, user_context=mood_context())
    if response:
        # Save prediction to check later
        with open(PREDICTION_FILE, "w") as f:
            json.dump({"race_num": race_num, "prediction": response, "correct": None}, f)

        embed = discord.Embed(
            title=f"🔮 Dale's Race {race_num} Prediction",
            description=response,
            color=0xFFD700,
            timestamp=datetime.utcnow()
        )
        embed.set_footer(text="Hold Dale accountable after the race 👀")
        await ch.send(embed=embed)


@bot.event
async def on_ready():
    print(f"✅  Ask Dale Bot online as {bot.user}")
    race_reminder.start()
    dales_weekly_take.start()
    pre_race_trash_talk.start()
    race_prediction.start()
    await bot.change_presence(activity=discord.Game("QSR Full Throttle Series 🏁"))
    if ANTHROPIC_API_KEY:
        print("✅  Claude AI enabled — Ask Dale is fully intelligent!")
    else:
        print("⚠️  No ANTHROPIC_API_KEY — using FAQ mode only")
@bot.event
async def on_ready():
    print(f"✅  Ask Dale Bot online as {bot.user}")
    race_reminder.start()
    dales_weekly_take.start()
    pre_race_trash_talk.start()
    race_prediction.start()
    race_announcement_scheduler.start()  # ← ADD THIS LINE
    await bot.change_presence(activity=discord.Game("QSR Full Throttle Series 🏁"))

@bot.event
async def on_message(message: discord.Message):
    """
    Dale responds to:
    1. Any message in #ask-dale channel
    2. Any @ mention anywhere in the server
    3. !ask and !dale commands
    """
    # Ignore other bots
    if message.author.bot:
        return

    # Determine if Dale should respond
    in_ask_dale_channel = (message.channel.name == ASK_DALE_CH)
    was_mentioned = bot.user in message.mentions
    is_command = message.content.startswith("!")

    # Don't respond to commands in ask-dale channel (let command handler do it)
    if in_ask_dale_channel and is_command:
        await bot.process_commands(message)
        return

    should_respond = in_ask_dale_channel or was_mentioned

    if should_respond:
        # Build the question
        question = message.content
        # Strip mention if present
        for mention in [f"<@{bot.user.id}>", f"<@!{bot.user.id}>"]:
            question = question.replace(mention, "").strip()

        if not question:
            question = "hey"

        q_lower = question.lower()

        # Daytona 2001 — hardcoded sensitive response
        daytona_keywords = ["daytona 2001", "february 18", "february 2001",
                           "how did you die", "crash 2001", "dale died",
                           "earnhardt died", "2001 crash", "that crash"]
        if any(kw in q_lower for kw in daytona_keywords):
            embed = discord.Embed(
                description="...I don't much like talkin' about that day. "
                           "Some things you just carry. "
                           "We ain't doin' this. 🏁",
                color=0x222222
            )
            embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series")
            await message.reply(embed=embed)
            await bot.process_commands(message)
            return

        async with message.channel.typing():
            if ANTHROPIC_API_KEY:
                channel_id = message.channel.id

                # Read Discord message history for context (last 15 messages)
                discord_history = []
                try:
                    async for msg in message.channel.history(limit=15, before=message):
                        if msg.author.bot and msg.author == bot.user:
                            # Dale's previous message
                            # Strip embed description if present
                            msg_content = msg.content
                            if msg.embeds:
                                msg_content = msg.embeds[0].description or msg.content
                            discord_history.insert(0, {
                                "role": "assistant",
                                "content": msg_content
                            })
                        elif not msg.author.bot:
                            # Member's previous message
                            discord_history.insert(0, {
                                "role": "user",
                                "content": f"{msg.author.display_name}: {msg.content}"
                            })
                except Exception as e:
                    print(f"History read error: {e}")

                # Merge Discord history with our stored history
                # Discord history takes priority as it's the actual channel content
                combined_history = discord_history[-10:] if discord_history else get_history(channel_id)

                # Add extra context if this is a reply to a specific message
                if message.reference and message.reference.resolved:
                    ref = message.reference.resolved
                    ref_content = ref.content
                    if ref.embeds:
                        ref_content = ref.embeds[0].description or ref.content
                    # Prepend the replied-to message so Dale knows exactly what's being referenced
                    question = f'[Replying to: "{ref_content}"]\n{question}'

                # Get Dale's memory of this user
                user_ctx = get_user_context(message.author.id, message.author.display_name)
                response = await ask_claude(question, channel_id, combined_history, user_ctx)
                if response:
                    add_to_history(channel_id, "user", question)
                    add_to_history(channel_id, "assistant", response)
                    # Update Dale's memory of this user
                    update_user_memory(message.author.id, message.author.display_name, question, response)
                    embed = discord.Embed(description=response, color=0xE8272A)
                    embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series 🏁")
                    await message.reply(embed=embed)
                    await bot.process_commands(message)
                    return

            # Fallback FAQ if no API
            for key, answer in FAQ.items():
                if key in q_lower:
                    embed = discord.Embed(description=answer, color=0xE8272A)
                    embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series 🏁")
                    await message.reply(embed=embed)
                    await bot.process_commands(message)
                    return

            # Smart fallback — FAQ based on keywords, otherwise rotating Dale lines
            responded = False
            for key, answer in FAQ.items():
                if key in q_lower:
                    embed = discord.Embed(description=answer, color=0xE8272A)
                    embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series 🏁")
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
                "**QSR Full Throttle Series** — We run the ARCA car at full 110% horsepower. "
                "No restrictions. Real power. Real racin'.\n\n"
                "**Here's what you need to do:**\n"
                "1️⃣  Read the rules → `#league-rules`\n"
                "2️⃣  Claim your number → `#number-request`\n"
                "3️⃣  Sign up for the next race → `#registration`\n\n"
                "Any questions, you ask Dale in `#ask-dale`. "
                "I'll tell you straight. See you on the track, son. 🏁"
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
            "Well shoot, you gotta ask me somethin\' son. Try:\n"
            "`!ask how do stage points work`\n"
            "`!ask what is bump drafting`\n"
            "`!ask how do I protest someone`\n"
            "`!ask what is iRating`\n"
            "`!ask tips for drafting on ovals`"
        )
        return

    # Show typing indicator while thinking
    async with ctx.typing():
        # Special handling for sensitive topics
        q_lower = question.lower()
        daytona_keywords = ["daytona 2001", "february 18", "february 2001", "how did you die",
                           "crash 2001", "dale died", "earnhardt died", "the crash"]
        if any(kw in q_lower for kw in daytona_keywords):
            embed = discord.Embed(
                description="...I don't much like talkin' about that day. Some things you just carry with you. "
                           "Let's talk about somethin' else. 🏁",
                color=0x333333
            )
            embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series")
            await ctx.send(embed=embed)
            return

        # Try Claude AI first
        if ANTHROPIC_API_KEY:
            response = await ask_claude(question)
            if response:
                embed = discord.Embed(
                    description=response,
                    color=0xE8272A
                )
                embed.set_footer(text="Ask Dale #3 | QSR Full Throttle Series 🏁")
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
            f"I\'ll be honest with ya, I ain\'t got a good answer for that one. "
            f"Head on over to `#help-desk` or tag an @Admin and they\'ll sort you out. "
            f"Ask me somethin\' about racin\' though — that I can handle. 🏁"
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
        title=f"🏁 Ask Dale #3 — The Intimidator Bot [{ai_status}]",
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

@bot.command(name="dalemem")
@is_owner()
async def dale_memory_cmd(ctx, *, username: str = ""):
    """View or manage Dale's user memory. Usage: !dalemem OR !dalemem reset @user"""
    memory = load_user_memory()
    
    if "reset" in username.lower() and ctx.message.mentions:
        # Reset a specific user
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


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("🚫 You don't have permission to use that command.")
    elif isinstance(error, commands.CommandNotFound):
        pass
    else:
        await ctx.send(f"⚠️ Error: {error}")


# ─────────────────────────────────────────────────────────────────
#  ADMIN — TRIGGER NEWCOMER CALLOUT MANUALLY
# ─────────────────────────────────────────────────────────────────

@bot.command(name="newcomer")
@is_owner()
async def newcomer_cmd(ctx, *, driver_name: str):
    """
    Trigger Dale's newcomer callout for a new driver.
    Usage: !newcomer Norman King
    """
    guild = bot.get_guild(GUILD_ID)
    await newcomer_callout(guild, driver_name)
    await ctx.send(f"✅ Dale welcomed {driver_name} to the garage!")


@bot.command(name="dalerecap")
@is_owner()
async def dale_recap_cmd(ctx):
    """Trigger Dale's post-race reaction based on most recent race data."""
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


@bot.command(name="dalemood")
@is_owner()
async def dale_mood_cmd(ctx, mood: str = ""):
    """View or set Dale's mood. Moods: neutral, good, grumpy, fired_up"""
    if mood in ["neutral", "good", "grumpy", "fired_up"]:
        set_dale_mood(mood, "Manually set by admin")
        await ctx.send(f"✅ Dale's mood set to `{mood}`")
    else:
        current = get_dale_mood()
        await ctx.send(f"Dale's current mood: `{current}`\nOptions: `neutral` `good` `grumpy` `fired_up`")

# ─────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────
#  RACE ANNOUNCEMENT SCHEDULER
#  Posts every Monday at 12PM ET to #series-announcements
#  Channel ID: 1173977366117232731 | @arca Role ID: 1173980377279377538
# ─────────────────────────────────────────────────────────────────

ANNOUNCEMENT_CHANNEL_ID = 1173977366117232731
ARCA_ROLE_ID            = 1173980377279377538
POSTED_FILE             = "posted_announcements.json"

RACE_ANNOUNCEMENTS = [
    {
        "date": "2026-07-20",
        "track": "Daytona International Speedway",
        "race_num": 1,
        "laps": 80,
        "message": (
            "🏁 **RACE 1 — DAYTONA INTERNATIONAL SPEEDWAY**\n"
            "<@&1173980377279377538>\n\n"
            "The QSR Full Throttle Series fires up tonight at the Great American Speedway. "
            "Daytona. Where legends are made and seasons are defined. "
            "You want to set the tone for the whole year? Tonight's your shot.\n\n"
            "🗓️ **Monday, July 20 | 8:00 PM ET**\n"
            "🚗 ARCA Menards | 110% HP | No Restrictions\n"
            "🏆 40 pts on the line — Championship starts NOW\n\n"
            "Register in `#registration` | Questions? Ask Dale in `#ask-dale`\n"
            "📺 Stream: `#how-to-watch`\n\n"
            "Let's go racing. 🔥"
        )
    },
    {
        "date": "2026-07-27",
        "track": "Bristol Motor Speedway",
        "race_num": 2,
        "laps": 200,
        "message": (
            "🏁 **RACE 2 — BRISTOL MOTOR SPEEDWAY**\n"
            "<@&1173980377279377538>\n\n"
            "The Concrete Colosseum. Half-mile of pure chaos. "
            "Bristol separates the racers from the crashers — and tonight we find out which one you are.\n\n"
            "🗓️ **Monday, July 27 | 8:00 PM ET**\n"
            "🚗 ARCA Menards | 110% HP | No Restrictions\n"
            "🏆 40 pts | Race 2 of 14\n\n"
            "Register in `#registration` | Questions? Ask Dale in `#ask-dale`\n"
            "📺 Stream: `#how-to-watch`\n\n"
            "Bristol doesn't lie. Neither does the scoreboard. 🔥"
        )
    },
    {
        "date": "2026-08-03",
        "track": "Atlanta Motor Speedway",
        "race_num": 3,
        "laps": 130,
        "message": (
            "🏁 **RACE 3 — ATLANTA MOTOR SPEEDWAY**\n"
            "<@&1173980377279377538>\n\n"
            "Wide open, mile-and-a-half, pack racing at its finest. "
            "Atlanta rewards the brave and punishes the timid. "
            "If you've got it — tonight's where you show it.\n\n"
            "🗓️ **Monday, August 3 | 8:00 PM ET**\n"
            "🚗 ARCA Menards | 110% HP | No Restrictions\n"
            "🏆 40 pts | Race 3 of 14\n\n"
            "Register in `#registration` | Questions? Ask
bot.run(BOT_TOKEN)
