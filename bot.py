import discord
from discord import app_commands
from discord.ext import commands, tasks
import psycopg2
import os
import re
import io
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

# ===== TABLES =====
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
    res = youtube.videos().list(part="snippet,statistics", id=video_id).execute()
    if not res["items"]:
        return None
    return res["items"][0]

def get_channel_id_from_url(url):
    try:
        url = url.split("?")[0]

        if "@" in url:
            handle = url.split("@")[1]
            res = youtube.channels().list(part="snippet", forHandle=handle).execute()
            if not res["items"]:
                return None, None
            channel = res["items"][0]
            return channel["id"], channel["snippet"]["title"]

        if "channel/" in url:
            channel_id = url.split("channel/")[1]
            res = youtube.channels().list(part="snippet", id=channel_id).execute()
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
    print(f"✅ Logged in as {bot.user}")

# ===== USER LOG (FIXED) =====
@tree.command(name="user_log", description="View a user's submissions (MOD ONLY)")
async def user_log(interaction: discord.Interaction, member: discord.Member):

    MOD_ROLE_ID = 1491424019877200013

    if not any(role.id == MOD_ROLE_ID for role in interaction.user.roles):
        await interaction.response.send_message(
            embed=discord.Embed(description="❌ You are not a MOD", color=discord.Color.red()),
            ephemeral=True
        )
        return

    user_id = str(member.id)

    cursor.execute("SELECT channel_id, channel_name FROM users WHERE user_id=%s", (user_id,))
    channels = cursor.fetchall()

    if not channels:
        await interaction.response.send_message(
            embed=discord.Embed(description="❌ No linked channels", color=discord.Color.red()),
            ephemeral=True
        )
        return

    content = f"USER REPORT\nUser: {member.name}\n\n"

    total_views = 0
    total_likes = 0

    for idx, (channel_id, channel_name) in enumerate(channels, start=1):

        content += f"====================\nCHANNEL {idx}\n====================\n"
        content += f"Name: {channel_name}\nID: {channel_id}\n\n"

        cursor.execute(
            "SELECT link, views, likes FROM submissions WHERE user_id=%s AND channel_name=%s",
            (user_id, channel_name)
        )
        videos = cursor.fetchall()

        ch_views = 0
        ch_likes = 0

        for i, (link, views, likes) in enumerate(videos, start=1):
            content += f"{i}.\n{link}\nViews: {views}\nLikes: {likes}\n\n"
            ch_views += views
            ch_likes += likes

        content += f"Channel Total Views: {ch_views}\nChannel Total Likes: {ch_likes}\n\n"

        total_views += ch_views
        total_likes += ch_likes

    content += f"====================\nOVERALL\n====================\n"
    content += f"Total Views: {total_views}\nTotal Likes: {total_likes}"

    # ✅ FIXED LINE
    file = discord.File(
        fp=io.BytesIO(content.encode()),
        filename=f"{member.name}_log.txt"
    )

    await interaction.response.send_message(file=file, ephemeral=True)

# ===== LINK =====
@tree.command(name="link_youtube", description="Link your YouTube channel")
async def link_youtube(interaction: discord.Interaction, channel_url: str):

    await interaction.response.defer(ephemeral=True)

    channel_id, channel_name = get_channel_id_from_url(channel_url)

    if not channel_id:
        await interaction.followup.send(embed=discord.Embed(description="❌ Invalid channel link", color=discord.Color.red()), ephemeral=True)
        return

    cursor.execute("SELECT user_id FROM users WHERE channel_id=%s", (channel_id,))
    existing = cursor.fetchone()

    if existing and existing[0] != str(interaction.user.id):
        await interaction.followup.send(embed=discord.Embed(description="❌ Already linked to another user", color=discord.Color.red()), ephemeral=True)
        return

    cursor.execute("SELECT COUNT(*) FROM users WHERE user_id=%s", (str(interaction.user.id),))
    count = cursor.fetchone()[0]

    if count >= 2:
        await interaction.followup.send(embed=discord.Embed(description="❌ Max 2 channels", color=discord.Color.red()), ephemeral=True)
        return

    cursor.execute(
        "INSERT INTO users (user_id, channel_id, channel_name) VALUES (%s, %s, %s)",
        (str(interaction.user.id), channel_id, channel_name)
    )
    conn.commit()

    await interaction.followup.send(embed=discord.Embed(description=f"✅ Linked: {channel_name}", color=discord.Color.green()), ephemeral=True)

# ===== SUBMIT =====
@tree.command(name="submit", description="Submit your YouTube video")
async def submit(interaction: discord.Interaction, url: str):

    await interaction.response.defer(ephemeral=True)

    try:
        video_id = extract_video_id(url)

        if not video_id:
            await interaction.followup.send(embed=discord.Embed(description="❌ Invalid YouTube link", color=discord.Color.red()), ephemeral=True)
            return

        data = get_video_stats(video_id)
        if not data:
            await interaction.followup.send(embed=discord.Embed(description="❌ Failed to fetch video", color=discord.Color.red()), ephemeral=True)
            return

        video_channel_id = data['snippet']['channelId']
        channel_name = data['snippet']['channelTitle']
        views = int(data['statistics'].get('viewCount', 0))
        likes = int(data['statistics'].get('likeCount', 0))

        cursor.execute("SELECT channel_id FROM users WHERE user_id=%s", (str(interaction.user.id),))
        results = cursor.fetchall()

        if not results:
            await interaction.followup.send(embed=discord.Embed(description="❌ Link YouTube first", color=discord.Color.red()), ephemeral=True)
            return

        linked_channels = [r[0] for r in results]

        if video_channel_id not in linked_channels:
            await interaction.followup.send(embed=discord.Embed(description="❌ Not your channel", color=discord.Color.red()), ephemeral=True)
            return

        cursor.execute("SELECT 1 FROM submissions WHERE video_id=%s", (video_id,))
        if cursor.fetchone():
            await interaction.followup.send(embed=discord.Embed(description="❌ Already submitted", color=discord.Color.red()), ephemeral=True)
            return

        cursor.execute(
            "INSERT INTO submissions (user_id, video_id, link, channel_name, views, likes) VALUES (%s, %s, %s, %s, %s, %s)",
            (str(interaction.user.id), video_id, url, channel_name, views, likes)
        )
        conn.commit()

        embed = discord.Embed(title="📺 Video Added", color=discord.Color.green())
        embed.add_field(name="Channel", value=channel_name)
        embed.add_field(name="👁 Views", value=f"{views:,}")
        embed.add_field(name="❤️ Likes", value=f"{likes:,}")

        await interaction.followup.send(embed=embed, ephemeral=True)

    except Exception as e:
        conn.rollback()
        print("ERROR:", e)

        await interaction.followup.send(
            embed=discord.Embed(description="❌ Something went wrong", color=discord.Color.red()),
            ephemeral=True
        )

# ===== STATS =====
@tree.command(name="stats", description="Your stats")
async def stats(interaction: discord.Interaction):

    cursor.execute("""
    SELECT channel_name, SUM(views), SUM(likes)
    FROM submissions
    WHERE user_id=%s
    GROUP BY channel_name
    """, (str(interaction.user.id),))

    rows = cursor.fetchall()

    if not rows:
        await interaction.response.send_message(embed=discord.Embed(description="❌ No data", color=discord.Color.red()), ephemeral=True)
        return

    cursor.execute("SELECT SUM(views), SUM(likes) FROM submissions WHERE user_id=%s", (str(interaction.user.id),))
    total = cursor.fetchone()

    embed = discord.Embed(title="📊 Your Stats", color=discord.Color.blue())

    for name, views, likes in rows:
        embed.add_field(name=f"📢 {name}", value=f"👁 {views:,} | ❤️ {likes:,}", inline=False)

    embed.add_field(name="🔥 TOTAL", value=f"👁 {total[0]:,} | ❤️ {total[1]:,}", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

bot.run(TOKEN)