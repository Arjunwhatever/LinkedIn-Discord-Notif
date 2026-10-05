"""
Discord bot that auto-posts new content to a channel, per-server, for:
  - LinkedIn company pages (via linkedin_scraper.py — no login, no API key)
  - YouTube channels (via RSS — no API key)

Each server configures itself with slash commands, so one bot install can
serve many servers tracking different pages/channels — the same pattern as
installable YouTube/RSS bots in Discord's App Directory.

Setup:
  1. pip install -r requirements.txt
  2. Copy .env.example -> .env and fill in DISCORD_TOKEN.
  3. python bot_new.py
  4. In your server: /setup channel, then /setup-linkedin url:<company page>
     and/or /setup-youtube channel_id:<UC...>.

LinkedIn scraping is against LinkedIn's terms and can break or get blocked
at any time (see linkedin_scraper.py). Keep polling infrequent.
"""

import asyncio
import os
import sqlite3

import discord
import feedparser
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

from linkedin_scraper import LinkedInBlocked, fetch_posts

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
LINKEDIN_POLL_MINUTES = int(os.getenv("LINKEDIN_POLL_MINUTES", "60"))
YOUTUBE_POLL_MINUTES = int(os.getenv("YOUTUBE_POLL_MINUTES", "10"))
# Seconds to wait between scraping each configured LinkedIn page in one poll pass,
# so a bot tracking many servers doesn't hammer LinkedIn all at once.
LINKEDIN_PAGE_DELAY_SECONDS = int(os.getenv("LINKEDIN_PAGE_DELAY_SECONDS", "15"))

DB_PATH = "bot_data.db"

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- storage ----------

def db():
    return sqlite3.connect(DB_PATH)


def init_db():
    conn = db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS guild_config (
            guild_id INTEGER PRIMARY KEY,
            channel_id INTEGER,
            linkedin_url TEXT,
            youtube_channel_id TEXT
        )"""
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_linkedin (guild_id INTEGER, post_key TEXT, "
        "PRIMARY KEY (guild_id, post_key))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS seen_youtube (guild_id INTEGER, video_id TEXT, "
        "PRIMARY KEY (guild_id, video_id))"
    )
    conn.commit()
    conn.close()


def _ensure_row(conn, guild_id: int):
    conn.execute(
        "INSERT OR IGNORE INTO guild_config (guild_id) VALUES (?)", (guild_id,)
    )


def set_channel(guild_id: int, channel_id: int):
    conn = db()
    _ensure_row(conn, guild_id)
    conn.execute(
        "UPDATE guild_config SET channel_id = ? WHERE guild_id = ?", (channel_id, guild_id)
    )
    conn.commit()
    conn.close()


def set_linkedin_url(guild_id: int, url: str):
    conn = db()
    _ensure_row(conn, guild_id)
    conn.execute(
        "UPDATE guild_config SET linkedin_url = ? WHERE guild_id = ?", (url, guild_id)
    )
    conn.commit()
    conn.close()


def set_youtube_channel(guild_id: int, yt_channel_id: str):
    conn = db()
    _ensure_row(conn, guild_id)
    conn.execute(
        "UPDATE guild_config SET youtube_channel_id = ? WHERE guild_id = ?",
        (yt_channel_id, guild_id),
    )
    conn.commit()
    conn.close()


def clear_linkedin(guild_id: int):
    conn = db()
    conn.execute(
        "UPDATE guild_config SET linkedin_url = NULL WHERE guild_id = ?", (guild_id,)
    )
    conn.execute("DELETE FROM seen_linkedin WHERE guild_id = ?", (guild_id,))
    conn.commit()
    conn.close()


def clear_youtube(guild_id: int):
    conn = db()
    conn.execute(
        "UPDATE guild_config SET youtube_channel_id = NULL WHERE guild_id = ?", (guild_id,)
    )
    conn.execute("DELETE FROM seen_youtube WHERE guild_id = ?", (guild_id,))
    conn.commit()
    conn.close()


def get_guild_config(guild_id: int):
    conn = db()
    row = conn.execute(
        "SELECT channel_id, linkedin_url, youtube_channel_id FROM guild_config WHERE guild_id = ?",
        (guild_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"channel_id": row[0], "linkedin_url": row[1], "youtube_channel_id": row[2]}


def all_guild_configs():
    conn = db()
    rows = conn.execute(
        "SELECT guild_id, channel_id, linkedin_url, youtube_channel_id FROM guild_config"
    ).fetchall()
    conn.close()
    return [
        {"guild_id": r[0], "channel_id": r[1], "linkedin_url": r[2], "youtube_channel_id": r[3]}
        for r in rows
    ]


def is_linkedin_seen(guild_id: int, key: str) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM seen_linkedin WHERE guild_id = ? AND post_key = ?", (guild_id, key)
    ).fetchone()
    conn.close()
    return row is not None


def mark_linkedin_seen(guild_id: int, key: str):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO seen_linkedin (guild_id, post_key) VALUES (?, ?)", (guild_id, key)
    )
    conn.commit()
    conn.close()


def is_youtube_seen(guild_id: int, video_id: str) -> bool:
    conn = db()
    row = conn.execute(
        "SELECT 1 FROM seen_youtube WHERE guild_id = ? AND video_id = ?", (guild_id, video_id)
    ).fetchone()
    conn.close()
    return row is not None


def mark_youtube_seen(guild_id: int, video_id: str):
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO seen_youtube (guild_id, video_id) VALUES (?, ?)", (guild_id, video_id)
    )
    conn.commit()
    conn.close()


# ---------- helpers ----------

def linkedin_embed(post: dict) -> discord.Embed:
    text = post.get("text") or ""
    snippet = text[:500] + ("…" if len(text) > 500 else "")
    embed = discord.Embed(
        title="💼 New LinkedIn post",
        description=snippet or "(no caption)",
        url=post.get("url"),
        color=discord.Color.blue(),
    )
    return embed


def youtube_embed(entry) -> discord.Embed:
    return discord.Embed(
        title=f"📺 New upload: {entry.title}",
        url=entry.link,
        color=discord.Color.red(),
    )


def fetch_youtube_entries(yt_channel_id: str):
    feed = feedparser.parse(
        f"https://www.youtube.com/feeds/videos.xml?channel_id={yt_channel_id}"
    )
    return feed.entries


# ---------- slash commands ----------

@bot.tree.command(name="setup", description="Set this channel as the destination for announcements")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_channel(interaction: discord.Interaction):
    set_channel(interaction.guild_id, interaction.channel_id)
    await interaction.response.send_message(
        f"✅ Announcements will be posted in {interaction.channel.mention}.", ephemeral=True
    )


@bot.tree.command(name="setup-linkedin", description="Track a LinkedIn company page for new posts")
@app_commands.describe(url="The company page URL, e.g. https://www.linkedin.com/company/acme/")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_linkedin(interaction: discord.Interaction, url: str):
    if "linkedin.com/company/" not in url:
        await interaction.response.send_message(
            "That doesn't look like a company page URL — expecting something like "
            "`https://www.linkedin.com/company/<slug>/`.",
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)
    set_linkedin_url(interaction.guild_id, url)

    try:
        posts = await asyncio.to_thread(fetch_posts, url)
    except LinkedInBlocked as e:
        await interaction.followup.send(
            f"Saved the URL, but the first fetch was blocked by LinkedIn ({e}). "
            "I'll keep retrying on the regular poll schedule.",
            ephemeral=True,
        )
        return
    except Exception as e:
        await interaction.followup.send(
            f"Saved the URL, but something went wrong fetching it: {e}. Will retry on schedule.",
            ephemeral=True,
        )
        return

    for p in posts:
        mark_linkedin_seen(interaction.guild_id, p["key"])

    if not posts:
        await interaction.followup.send(
            "Saved. Fetched the page but found no posts right now — LinkedIn sometimes hides "
            "posts from logged-out visitors. I'll keep checking.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ Tracking {url}. Found {len(posts)} existing post(s), marked as seen — "
        "only posts published from now on will be announced.",
        ephemeral=True,
    )


@bot.tree.command(name="setup-youtube", description="Track a YouTube channel for new uploads")
@app_commands.describe(channel_id="The channel ID, starts with UC... (not the @handle)")
@app_commands.checks.has_permissions(manage_guild=True)
async def setup_youtube(interaction: discord.Interaction, channel_id: str):
    await interaction.response.defer(ephemeral=True)
    set_youtube_channel(interaction.guild_id, channel_id)

    entries = await asyncio.to_thread(fetch_youtube_entries, channel_id)
    for entry in entries:
        mark_youtube_seen(interaction.guild_id, entry.yt_videoid)

    if not entries:
        await interaction.followup.send(
            "Saved, but couldn't find any videos for that channel ID — double check it starts "
            "with `UC`.",
            ephemeral=True,
        )
        return

    await interaction.followup.send(
        f"✅ Tracking YouTube channel `{channel_id}`. Found {len(entries)} existing video(s), "
        "marked as seen.",
        ephemeral=True,
    )


@bot.tree.command(name="announce", description="Manually post an update to this server's announcement channel")
@app_commands.describe(platform="Where this update is from", url="Link to the post", note="Optional note")
@app_commands.choices(
    platform=[
        app_commands.Choice(name="LinkedIn", value="LinkedIn"),
        app_commands.Choice(name="YouTube", value="YouTube"),
        app_commands.Choice(name="Other", value="Other"),
    ]
)
async def announce(
    interaction: discord.Interaction,
    platform: app_commands.Choice[str],
    url: str,
    note: str = None,
):
    cfg = get_guild_config(interaction.guild_id)
    if not cfg or not cfg["channel_id"]:
        await interaction.response.send_message(
            "Run `/setup` in the channel you want to use first.", ephemeral=True
        )
        return

    channel = bot.get_channel(cfg["channel_id"])
    if channel is None:
        await interaction.response.send_message(
            "The configured channel couldn't be found. Run `/setup` again.", ephemeral=True
        )
        return

    emoji = {"LinkedIn": "💼", "YouTube": "📺", "Other": "📣"}.get(platform.value, "📣")
    embed = discord.Embed(
        title=f"{emoji} New {platform.value} post", description=note or "", url=url,
        color=discord.Color.blue(),
    )
    await channel.send(embed=embed)
    await interaction.response.send_message("✅ Posted!", ephemeral=True)


@bot.tree.command(name="remove-linkedin", description="Stop tracking this server's LinkedIn company page")
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_linkedin(interaction: discord.Interaction):
    cfg = get_guild_config(interaction.guild_id)
    if not cfg or not cfg["linkedin_url"]:
        await interaction.response.send_message("No LinkedIn page is currently tracked.", ephemeral=True)
        return
    clear_linkedin(interaction.guild_id)
    await interaction.response.send_message("✅ Stopped tracking LinkedIn for this server.", ephemeral=True)


@bot.tree.command(name="remove-youtube", description="Stop tracking this server's YouTube channel")
@app_commands.checks.has_permissions(manage_guild=True)
async def remove_youtube(interaction: discord.Interaction):
    cfg = get_guild_config(interaction.guild_id)
    if not cfg or not cfg["youtube_channel_id"]:
        await interaction.response.send_message("No YouTube channel is currently tracked.", ephemeral=True)
        return
    clear_youtube(interaction.guild_id)
    await interaction.response.send_message("✅ Stopped tracking YouTube for this server.", ephemeral=True)


@bot.tree.command(name="status", description="Show this server's announcement configuration")
async def status(interaction: discord.Interaction):
    cfg = get_guild_config(interaction.guild_id)
    channel = f"<#{cfg['channel_id']}>" if cfg and cfg["channel_id"] else "not set (run /setup)"
    linkedin = cfg["linkedin_url"] if cfg and cfg["linkedin_url"] else "not tracked"
    youtube = cfg["youtube_channel_id"] if cfg and cfg["youtube_channel_id"] else "not tracked"
    await interaction.response.send_message(
        f"**Channel:** {channel}\n**LinkedIn:** {linkedin}\n**YouTube:** {youtube}",
        ephemeral=True,
    )


async def _perm_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need the **Manage Server** permission to run this.", ephemeral=True
        )
    else:
        raise error


setup_channel.error(_perm_error)
setup_linkedin.error(_perm_error)
setup_youtube.error(_perm_error)
remove_linkedin.error(_perm_error)
remove_youtube.error(_perm_error)


# ---------- polling loops ----------

@tasks.loop(minutes=LINKEDIN_POLL_MINUTES)
async def poll_linkedin():
    configs = [c for c in all_guild_configs() if c["linkedin_url"] and c["channel_id"]]
    page_cache: dict[str, list] = {}

    for cfg in configs:
        url = cfg["linkedin_url"]
        if url not in page_cache:
            try:
                page_cache[url] = await asyncio.to_thread(fetch_posts, url)
            except LinkedInBlocked as e:
                print(f"LinkedIn blocked for {url}: {e}")
                page_cache[url] = []
            except Exception as e:
                print(f"LinkedIn fetch failed for {url}: {e}")
                page_cache[url] = []
            await asyncio.sleep(LINKEDIN_PAGE_DELAY_SECONDS)

        posts = page_cache[url]
        new_posts = [p for p in posts if not is_linkedin_seen(cfg["guild_id"], p["key"])]
        if not new_posts:
            continue

        channel = bot.get_channel(cfg["channel_id"])
        if channel is not None:
            for p in reversed(new_posts):
                await channel.send(embed=linkedin_embed(p))

        for p in new_posts:
            mark_linkedin_seen(cfg["guild_id"], p["key"])


@tasks.loop(minutes=YOUTUBE_POLL_MINUTES)
async def poll_youtube():
    configs = [c for c in all_guild_configs() if c["youtube_channel_id"] and c["channel_id"]]
    feed_cache: dict[str, list] = {}

    for cfg in configs:
        yt_id = cfg["youtube_channel_id"]
        if yt_id not in feed_cache:
            feed_cache[yt_id] = await asyncio.to_thread(fetch_youtube_entries, yt_id)

        entries = feed_cache[yt_id]
        new_entries = [e for e in entries if not is_youtube_seen(cfg["guild_id"], e.yt_videoid)]
        if not new_entries:
            continue

        channel = bot.get_channel(cfg["channel_id"])
        if channel is not None:
            for entry in reversed(new_entries):
                await channel.send(embed=youtube_embed(entry))

        for entry in new_entries:
            mark_youtube_seen(cfg["guild_id"], entry.yt_videoid)


@poll_linkedin.before_loop
@poll_youtube.before_loop
async def before_polls():
    await bot.wait_until_ready()


# ---------- lifecycle ----------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    init_db()
    await bot.tree.sync()

    if not poll_linkedin.is_running():
        poll_linkedin.start()
    if not poll_youtube.is_running():
        poll_youtube.start()

    print("Ready. Slash commands synced; pollers running.")


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN in your .env file.")
    bot.run(DISCORD_TOKEN)
