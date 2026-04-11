import discord
from discord import app_commands
from discord.ext import commands, tasks
import psycopg2
import os
import re
from googleapiclient.discovery import build

# ===== ENV =====
TOKEN = os.getenv("TOKEN")
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY")
DATABASE_URL = os.getenv("DATABASE_URL")

# ===== BOT =====
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

youtube = build("youtube", "v3", developerKey=YOUTUBE_API_KEY)

# ===== DATABASE =====
conn = psycopg2.connect(DATABASE_URL)
cursor = conn.cursor()

# CREATE TABLES
cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    user_id TEXT,
    channel_id TEXT,
    channel_name TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS submissions (
    id SERIAL PRIMARY KEY,
    user_id TEXT,
    video_id TEXT,
    link TEXT,
    channel_name TEXT,
    views INTEGER,
    likes INTEGER,
    submitted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")

conn.commit()

# ===== HELPERS =====

def extract_video_id(url):
    match = re.search(r"(?:v=|shorts/|youtu\.be/)([0-9A-Za-z_-]{11})", url)
    return match.group(1) if match else None


def get_video_stats(video_id):
    res = youtube.videos().list(
        part="snippet,statistics",
        id=video_id
    ).execute()

    if not res["items"]:
        return None

    return res["items"][0]


def get_channel_id_from_url(url):
    try:
        url = url.split("?")[0]

        if "@" in url:
            handle = url.split("@")[1]

            res = youtube.channels().list(
                part="snippet",
                forHandle=handle
            ).execute()

            if not res["items"]:
                return None, None

            channel = res["items"][0]
            return channel["id"], channel["snippet"]["title"]

        if "channel/" in url:
            channel_id = url.split("channel/")[1]

            res = youtube.channels().list(
                part="snippet",
                id=channel_id
            ).execute()

            if not res["items"]:
                return None, None

            return channel_id, res["items"][0]["snippet"]["title"]

        return None, None

    except Exception as e:
        print("Channel fetch error:", e)
        return None, None


# ===== EVENTS =====

@bot.event
async def on_ready():
    await tree.sync()
    auto_refresh.start()
    print(f"✅ Logged in as {bot.user}")


# ===== COMMANDS =====

@tree.command(name="link_youtube", description="Link your YouTube channel")
async def link_youtube(interaction: discord.Interaction, channel_url: str):

    await interaction.response.defer()

    channel_id, channel_name = get_channel_id_from_url(channel_url)

    if not channel_id:
        await interaction.followup.send("❌ Invalid channel link")
        return

    # remove old
    cursor.execute("DELETE FROM users WHERE user_id=%s", (str(interaction.user.id),))

    cursor.execute(
        "INSERT INTO users (user_id, channel_id, channel_name) VALUES (%s, %s, %s)",
        (str(interaction.user.id), channel_id, channel_name)
    )
    conn.commit()

    await interaction.followup.send(f"✅ Linked YouTube: {channel_name}")


@tree.command(name="submit", description="Submit your YouTube video")
async def submit(interaction: discord.Interaction, url: str):

    await interaction.response.defer()

    try:
        video_id = extract_video_id(url)

        if not video_id:
            await interaction.followup.send("❌ Invalid YouTube link")
            return

        data = get_video_stats(video_id)
        if not data:
            await interaction.followup.send("❌ Failed to fetch video")
            return

        video_channel_id = data['snippet']['channelId']
        channel_name = data['snippet']['channelTitle']
        views = int(data['statistics'].get('viewCount', 0))
        likes = int(data['statistics'].get('likeCount', 0))

        cursor.execute(
            "SELECT channel_id FROM users WHERE user_id=%s",
            (str(interaction.user.id),)
        )
        result = cursor.fetchone()

        if not result:
            await interaction.followup.send("❌ Link your YouTube first")
            return

        linked_channel_id = result[0]

        if video_channel_id != linked_channel_id:
            await interaction.followup.send("❌ Not your channel")
            return

        cursor.execute(
            "INSERT INTO submissions (user_id, video_id, link, channel_name, views, likes) VALUES (%s, %s, %s, %s, %s, %s)",
            (str(interaction.user.id), video_id, url, channel_name, views, likes)
        )
        conn.commit()

        await interaction.followup.send(
            f"📺 Video Added\n👁 {views:,} | ❤️ {likes:,}"
        )

    except Exception as e:
        print("ERROR:", e)
        await interaction.followup.send("❌ Something went wrong")


@tree.command(name="stats", description="Your total stats")
async def stats(interaction: discord.Interaction):

    cursor.execute(
        "SELECT views, likes FROM submissions WHERE user_id=%s",
        (str(interaction.user.id),)
    )
    rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message("❌ No data")
        return

    total_views = sum(r[0] for r in rows)
    total_likes = sum(r[1] for r in rows)

    embed = discord.Embed(title="📊 Your Stats", color=discord.Color.red())
    embed.add_field(name="👁 Views", value=f"{total_views:,}")
    embed.add_field(name="❤️ Likes", value=f"{total_likes:,}")

    await interaction.response.send_message(embed=embed)


@tasks.loop(hours=1)
async def auto_refresh():
    print("🔄 Auto refreshing...")

    cursor.execute("SELECT video_id FROM submissions")
    videos = cursor.fetchall()

    for (video_id,) in videos:
        data = get_video_stats(video_id)
        if not data:
            continue

        views = int(data['statistics'].get('viewCount', 0))
        likes = int(data['statistics'].get('likeCount', 0))

        cursor.execute(
            "UPDATE submissions SET views=%s, likes=%s WHERE video_id=%s",
            (views, likes, video_id)
        )

    conn.commit()


bot.run(TOKEN)