import discord
from discord.ext import commands
import datetime
import pytz 
import motor.motor_asyncio
from discord import app_commands
import typing
import os 
from dotenv import load_dotenv # Used for reading .env file during local development
import threading 
import http.server 
import socketserver 

# --- Load Environment Variables ---
load_dotenv() 

# --- Configuration: GET SECRETS FROM ENVIRONMENT ---
MONGO_URI = os.environ.get("MONGO_URI") 
BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN") 
PORT = int(os.environ.get("PORT", 8080)) 

# Check if essential environment variables are set.
if not MONGO_URI or not BOT_TOKEN:
    print("FATAL ERROR: MONGO_URI and/or DISCORD_BOT_TOKEN environment variables not set.")
    exit(1) 

DB_NAME = "discord_bot_db" 
COLLECTION_NAME = "user_settings"

# --- Dummy Web Server to satisfy Render ---
def run_web_server():
    """Starts a minimal HTTP server to keep Render's Web Service alive."""
    
    class Handler(http.server.SimpleHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-type', 'text/plain')
            self.end_headers()
            self.wfile.write(b"Discord Bot is alive and running.")

    try:
        with socketserver.TCPServer(("", PORT), Handler) as httpd:
            print(f"Dummy Web Server listening on port {PORT}")
            httpd.serve_forever()
    except Exception as e:
        print(f"Error starting web server: {e}")

# --- Bot and Database Initialization ---
intents = discord.Intents.default()
# REQUIRED: Must enable this intent for !clear and !poll to read message content
intents.message_content = True 
intents.members = True # Best practice for moderation commands like !clear
bot = commands.Bot(command_prefix='!', intents=intents) 
tree = bot.tree 

# MongoDB Setup
try:
    client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]
    settings_collection = db[COLLECTION_NAME]
    print("MongoDB connection initiated.")
except Exception as e:
    print(f"Error connecting to MongoDB: {e}")

# Define the format choices for the /timestamp command
FORMAT_OPTIONS = [
    app_commands.Choice(name="Short Time (16:20)", value='t'),
    app_commands.Choice(name="Long Time (16:20:30)", value='T'),
    app_commands.Choice(name="Short Date (20/04/2021)", value='d'),
    app_commands.Choice(name="Long Date (20 April 2021)", value='D'),
    app_commands.Choice(name="Default Date/Time (20 April 2021 16:20)", value='f'),
    app_commands.Choice(name="Full Date/Time (Tuesday, 20 April 2021 16:20)", value='F'),
    app_commands.Choice(name="Relative Time (2 months ago)", value='R'),
]


# --- Bot Events ---

@bot.event
async def on_ready():
    """Sync slash commands when the bot is ready."""
    await bot.change_presence(activity=discord.Game(name="/timestamp | !poll"))
    
    try:
        synced = await tree.sync()
        print(f"Synced {len(synced)} command(s).")
    except Exception as e:
        print(f"Failed to sync slash commands: {e}")

    print(f'Bot is ready! Logged in as {bot.user}')


# --- Traditional Commands (Using ! Prefix) ---

@bot.command(name='clear')
@commands.has_permissions(manage_messages=True)
async def clear_messages(ctx, amount: int):
    """Deletes a specified number of messages (requires Manage Messages permission)."""
    if amount > 100:
        await ctx.send("I can only clear up to 100 messages at a time.")
        return
    # Delete 'amount' messages + 1 (to delete the command message itself)
    await ctx.channel.purge(limit=amount + 1) 
    await ctx.send(f'🧹 **{amount}** messages cleared by {ctx.author.mention}.', delete_after=5)

@bot.command(name='poll')
async def create_poll(ctx, question, *options):
    """Creates a poll with up to 10 options."""
    if len(options) > 10:
        await ctx.send("You can only provide up to 10 options for the poll.") 
        return
        
    # Standard reaction emojis for polls
    emojis = ['\u24C0', '\u24B7', '\u24B8', '\u24B9', '\u24BA', '\u24BB', '\u24BC', '\u24BD', '\u24BE', '\u24BF']
    
    poll_description = "".join([f'{emojis[i]} **{option}**\n' for i, option in enumerate(options)])
    
    embed = discord.Embed(
        title=f'📊 NEW POLL: {question}', 
        description=poll_description, 
        color=discord.Color.blue()
    )
    
    poll_message = await ctx.send(embed=embed)
    
    # Add reactions based on the number of options
    for i in range(len(options)):
        await poll_message.add_reaction(emojis[i])
        
    # Delete the user's command message
    await ctx.message.delete()


# --- Slash Commands: Timezone Management ---

@tree.command(name="timezone", description="Set your default timezone for timestamp generation.")
@app_commands.describe(timezone="The timezone name (e.g., Europe/Amsterdam, America/New_York)")
async def set_timezone_slash(interaction: discord.Interaction, timezone: str):
    await interaction.response.defer(ephemeral=True)

    try:
        pytz.timezone(timezone) # Validate the timezone string
    except pytz.exceptions.UnknownTimeZoneError:
        await interaction.followup.send(
            f"❌ Timezone Error: The timezone `{timezone}` is invalid. Please check the spelling. "
            f"Example: `Europe/London`.", 
            ephemeral=True
        )
        return

    # Update or insert user setting in the database
    await settings_collection.update_one(
        {"_id": interaction.user.id},
        {"$set": {"timezone": timezone}},
        upsert=True
    )
    
    await interaction.followup.send(
        f"✅ Your default timezone has been set to `{timezone}`.", 
        ephemeral=True
    )


# --- Slash Commands: Timestamp Generation ---

@tree.command(name="timestamp", description="Generate a Discord-compatible timestamp from a date/time.")
@app_commands.describe(
    date_time="The date and time (e.g., '2025-01-01 10:00', 'tomorrow 3pm').",
    timezone="Optional: The timezone for the input. Defaults to your saved timezone.",
    format_style="Optional: The desired display format."
)
@app_commands.choices(format_style=FORMAT_OPTIONS)
async def generate_timestamp_slash(
    interaction: discord.Interaction, 
    date_time: str, 
    timezone: typing.Optional[str] = None, 
    format_style: typing.Optional[app_commands.Choice[str]] = None
):
    await interaction.response.defer(ephemeral=True)
    
    default_zone_used = False
    user_tz = None

    try:
        # 1. Determine Timezone
        if not timezone:
            user_setting = await settings_collection.find_one({"_id": interaction.user.id})
            if user_setting and "timezone" in user_setting:
                user_tz = user_setting["timezone"]
                timezone = user_tz
                default_zone_used = True
            else:
                # Fallback to UTC if no timezone is provided or saved
                timezone = 'UTC' 

        # 2. Convert input to a datetime object
        tz = pytz.timezone(timezone)
        
        # Simple parsing logic for different formats
        try:
            # Try YYYY-MM-DD HH:MM format first
            dt_object = datetime.datetime.strptime(date_time, '%Y-%m-%d %H:%M')
        except ValueError:
            # Try DD-MM-YYYY HH:MM format (requested default date style)
            try:
                dt_object = datetime.datetime.strptime(date_time, '%d-%m-%Y %H:%M')
            except ValueError:
                # Try a common American format if the first fails
                try:
                    dt_object = datetime.datetime.strptime(date_time, '%m/%d/%Y %I:%M%p')
                except ValueError:
                     await interaction.followup.send(
                        f"❌ Date/Time Format Error: Could not parse `{date_time}`. "
                        f"Please use a format like `YYYY-MM-DD HH:MM` or `DD-MM-YYYY HH:MM` (e.g., `2025-12-31 23:59`).", 
                        ephemeral=True
                    )
                     return

        # 3. Localize and Convert to UTC
        localized_dt = tz.localize(dt_object, is_dst=None)
        utc_dt = localized_dt.astimezone(pytz.utc)
        
        # 4. Get Unix Timestamp and Format
        unix_timestamp = int(utc_dt.timestamp())
        style = format_style.value if format_style else 'F' 
        style_name = format_style.name if format_style else 'Full Date/Time'
        
        discord_format = f'<t:{unix_timestamp}:{style}>'
        
        # 5. Generate Output Embed
        embed = discord.Embed(
            title="⏱️ Generated Timestamp",
            description=(
                # Date format updated to strictly dd-mm-yyyy HH:MM
                f"**Input Time:** {localized_dt.strftime('%d-%m-%Y %H:%M')} {timezone.upper()} "
                f"{'(Your Default)' if default_zone_used else ''}\n"
                f"**Unix Time:** `{unix_timestamp}`"
            ),
            color=discord.Color.blue()
        )
        
        embed.add_field(
            name=f'Selected Format: {style_name} (`{style}`) ',
            value=f'**Code to Copy:**\n`{discord_format}`\n\n**Preview (in Discord):** {discord_format}',
            inline=False
        )
        
        await interaction.followup.send(embed=embed, ephemeral=True)
        
    except pytz.exceptions.UnknownTimeZoneError:
        await interaction.followup.send(
            f"❌ Timezone Error: The timezone `{timezone}` is invalid. Please check the spelling.", 
            ephemeral=True
        )
    except Exception as e:
        await interaction.followup.send(f"An unexpected error occurred: `{e}`", ephemeral=True)


# --- Run Bot ---

if __name__ == '__main__':
    # 1. Start the dummy web server in a separate thread
    server_thread = threading.Thread(target=run_web_server)
    server_thread.daemon = True 
    server_thread.start()
    
    # 2. Start the Discord Bot (This call blocks the main thread)
    try:
        bot.run(BOT_TOKEN)
    except Exception as e:
        print(f"Error running Discord bot: {e}")
