import os
import re
import time
import asyncio
import threading
import discord
from decorators import min_rank_required, has_allowed_role
from rate_limiter import RateLimiter
from discord import app_commands
from config import Config
from discord.ext import commands
from dotenv import load_dotenv
from flask import Flask
from typing import Optional, Set, Dict, List, Tuple
from roblox_commands import create_sc_command
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime

# --- Configuration ---
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Global rate limiter configuration
GLOBAL_RATE_LIMIT = 25  # requests per minute
COMMAND_COOLDOWN = 5    # seconds between command uses per user

# --- Utility Classes ---
class ReactionLogger:
    """Handles reaction monitoring and logging"""
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.monitor_channel_ids = set(Config.DEFAULT_MONITOR_CHANNELS)
        self.log_channel_id = Config.DEFAULT_LOG_CHANNEL
        self.rate_limiter = RateLimiter(calls_per_minute=GLOBAL_RATE_LIMIT)
        
    async def on_ready_setup(self):
        """Setup monitoring when bot starts"""
        guild = self.bot.guilds[0]  # For the first guild the bot is in
        
        # Verify channels exist
        valid_channels = set()
        for channel_id in self.monitor_channel_ids:
            if channel := guild.get_channel(channel_id):
                valid_channels.add(channel.id)
        
        self.monitor_channel_ids = valid_channels
        
        # Verify log channel exists
        if not guild.get_channel(self.log_channel_id):
            print(f"Warning: Default log channel {self.log_channel_id} not found!")
            self.log_channel_id = None

    async def _create_embed(self, title: str, description: str, 
                          color: discord.Color = discord.Color.blue(), 
                          ephemeral: bool = False) -> Dict:
        """Helper to create consistent embeds"""
        embed = discord.Embed(
            title=title,
            description=description,
            color=color
        )
        embed.set_footer(text=f"Executed at {time.strftime('%Y-%m-%d %H:%M:%S')}")
        return {"embed": embed, "ephemeral": ephemeral}

    async def _process_channels(self, interaction: discord.Interaction, 
                              input_str: str) -> Tuple[List[discord.TextChannel], List[str]]:
        """Process channel input string into valid channels and invalid names"""
        valid_channels = []
        invalid_names = []
        
        # Split by commas or spaces, and clean up
        channel_mentions = []
        current_mention = ""
        in_mention = False
        
        # Custom parsing to handle malformed mentions
        for char in input_str:
            if char == '<' and not in_mention:
                in_mention = True
                current_mention = "<"
            elif char == '>' and in_mention:
                current_mention += ">"
                channel_mentions.append(current_mention)
                current_mention = ""
                in_mention = False
            elif in_mention:
                current_mention += char
        
        # Also split by commas for regular names
        names = [n.strip() for n in input_str.split(',') if n.strip()]
        names.extend(channel_mentions)
        
        for name in names:
            try:
                if name.startswith('<#') and name.endswith('>'):
                    channel_id = int(name[2:-1])
                    if channel := interaction.guild.get_channel(channel_id):
                        valid_channels.append(channel)
                        continue
                
                if channel := discord.utils.get(interaction.guild.text_channels, name=name):
                    valid_channels.append(channel)
                else:
                    invalid_names.append(name)
            except ValueError:
                invalid_names.append(name)
                
        return valid_channels, invalid_names

    async def setup(self, interaction: discord.Interaction, 
                   log_channel: discord.TextChannel, 
                   monitor_channels: str):
        """Initialize reaction monitoring system"""
        await interaction.response.defer(ephemeral=True)  # Defer first
        
        channels, invalid = await self._process_channels(interaction, monitor_channels)
        
        if len(channels) > Config.MAX_MONITORED_CHANNELS:
            response = await self._create_embed(
                "⚠️ Channel Limit Exceeded",
                f"You can monitor up to {Config.MAX_MONITORED_CHANNELS} channels.",
                discord.Color.red(),
                True
            )
            await interaction.followup.send(**response)
            return

        self.monitor_channel_ids = {ch.id for ch in channels}
        self.log_channel_id = log_channel.id
        
        response = await self._create_embed(
            "✅ Setup Complete" if not invalid else "⚠️ Partial Setup",
            f"Now monitoring {len(channels)} channels" + 
            (f"\nCouldn't find: {', '.join(invalid)}" if invalid else ""),
            discord.Color.green() if not invalid else discord.Color.orange(),
            True
        )
        await interaction.followup.send(**response)

    async def add_channels(self, interaction: discord.Interaction, channels: str):
        """Add channels to monitor"""
        await interaction.response.defer(ephemeral=True)
        new_channels, invalid = await self._process_channels(interaction, channels)
        
        if len(self.monitor_channel_ids) + len(new_channels) > Config.MAX_MONITORED_CHANNELS:
            response = await self._create_embed(
                "⚠️ Channel Limit Exceeded",
                f"Cannot add {len(new_channels)} channels. Max is {Config.MAX_MONITORED_CHANNELS}.",
                discord.Color.red(),
                True
            )
            await interaction.followup.send(**response)
            return

        self.monitor_channel_ids.update(ch.id for ch in new_channels)
        
        response = await self._create_embed(
            "✅ Channels Added" if not invalid else "⚠️ Partial Success",
            f"Added {len(new_channels)} channels to monitoring" + 
            (f"\nCouldn't find: {', '.join(invalid)}" if invalid else ""),
            discord.Color.green() if not invalid else discord.Color.orange(),
            True
        )
        await interaction.followup.send(**response)

    async def remove_channels(self, interaction: discord.Interaction, channels: str):
        """Remove channels from monitoring"""
        await interaction.response.defer(ephemeral=True)
        remove_channels, invalid = await self._process_channels(interaction, channels)
        removed = []
        
        for ch in remove_channels:
            if ch.id in self.monitor_channel_ids:
                self.monitor_channel_ids.remove(ch.id)
                removed.append(ch.name)
        
        response = await self._create_embed(
            "✅ Channels Removed" if removed else "⚠️ No Channels Removed",
            (f"Stopped monitoring: {', '.join(removed)}" if removed else "No matching channels were being monitored") +
            (f"\nCouldn't find: {', '.join(invalid)}" if invalid else ""),
            discord.Color.green() if removed else discord.Color.orange(),
            True
        )
        await interaction.followup.send(**response)

    async def list_channels(self, interaction: discord.Interaction):
        """List currently monitored channels"""
        if not self.monitor_channel_ids:
            response = await self._create_embed(
                "ℹ️ No Channels Monitored",
                "Currently not monitoring any channels.",
                discord.Color.blue(),
                True
            )
            await interaction.response.send_message(**response)
            return

        channel_names = []
        guild = interaction.guild
        
        for channel_id in self.monitor_channel_ids:
            if channel := guild.get_channel(channel_id):
                channel_names.append(f"• {channel.mention}")
        
        response = await self._create_embed(
            "📋 Monitored Channels",
            "\n".join(channel_names) if channel_names else "No channels found",
            discord.Color.blue(),
            True
        )
        await interaction.response.send_message(**response)

    async def log_reaction(self, payload: discord.RawReactionActionEvent):
        """Log reactions from monitored channels (only for users with monitoring role)"""
        if (payload.channel_id not in self.monitor_channel_ids or 
            str(payload.emoji) not in Config.TRACKED_REACTIONS):
            return

        await self.rate_limiter.wait_if_needed()
            
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return

        member = guild.get_member(payload.user_id)
        if not member:
            return

        monitor_role = guild.get_role(Config.MONITOR_ROLE_ID)
        if not monitor_role or monitor_role not in member.roles:
            return

        channel = guild.get_channel(payload.channel_id)
        log_channel = guild.get_channel(self.log_channel_id)

        if not all((channel, member, log_channel)):
            return

        try:
            message = await channel.fetch_message(payload.message_id)
            content = (message.content[:100] + "...") if len(message.content) > 100 else message.content
                
            embed = discord.Embed(
                title="📝 Reaction Logged",
                description=f"{member.mention} (with {monitor_role.name} role) reacted with {payload.emoji}",
                color=discord.Color.blue()
            )
            
            embed.add_field(name="Channel", value=channel.mention)
            embed.add_field(name="Author", value=message.author.mention)
            embed.add_field(name="Message", value=content, inline=False)
            embed.add_field(name="Jump to", value=f"[Click here]({message.jump_url})", inline=False)
                
            await log_channel.send(embed=embed)
            await self.bot.sheets.update_points(member)  
        except discord.NotFound:
            return
        except Exception as e:
            print(f"[REACTION LOG ERROR] {type(e).__name__}: {str(e)}")
            
# --- Bot Initialization ---
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.guilds = True
intents.reactions = True

bot = commands.Bot(intents=intents, command_prefix="!")
bot.rate_limiter = RateLimiter(calls_per_minute=GLOBAL_RATE_LIMIT)
bot.reaction_logger = ReactionLogger(bot)

# --- Google Sheets Logic ---
class GoogleSheetsLogger:
    def __init__(self):
        try:
            self.scope = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive"
            ]
            
            # Get the private key and ensure proper newlines
            private_key = os.getenv("GS_PRIVATE_KEY")
            if private_key:
                private_key = private_key.replace('\\n', '\n')  # Convert escaped newlines
                
            creds_dict = {
                "type": os.getenv("GS_TYPE"),
                "project_id": os.getenv("GS_PROJECT_ID"),
                "private_key_id": os.getenv("GS_PRIVATE_KEY_ID"),
                "private_key": private_key,
                "client_email": os.getenv("GS_CLIENT_EMAIL"),
                "client_id": os.getenv("GS_CLIENT_ID"),
                "auth_uri": os.getenv("GS_AUTH_URI"),
                "token_uri": os.getenv("GS_TOKEN_URI"),
                "auth_provider_x509_cert_url": os.getenv("GS_AUTH_PROVIDER_CERT_URL"),
                "client_x509_cert_url": os.getenv("GS_CLIENT_CERT_URL")
            }
            
            # Validate all required fields are present
            if not all(creds_dict.values()):
                missing = [k for k, v in creds_dict.items() if not v]
                raise ValueError(f"Missing Google Sheets config: {missing}")
                
            self.creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, self.scope)
            self.client = gspread.authorize(self.creds)
            print("✅ Successfully connected to Google Sheets")
        except Exception as e:
            print(f"🔴 Google Sheets connection failed: {str(e)}")
            self.client = None

    async def update_points(self, member: discord.Member):
    if not self.client:
        print("Google Sheets client not initialized")
        return False

    try:
        sheet_id = os.getenv("GOOGLE_SHEET_ID")
        sheet_name = os.getenv("GOOGLE_SHEET_NAME", "Sheet1")
        
        print(f"Opening sheet {sheet_id}...")
        sheet = self.client.open_by_key(sheet_id)
        worksheet = sheet.worksheet(sheet_name)
        
        # Clean username
        username = re.sub(r'\[.*?\]', '', member.display_name).strip() or member.name
        print(f"Updating points for: {username}")
        
        # Get all records
        records = worksheet.get_all_records()
        print(f"Found {len(records)} existing records")
        
        # Find existing user
        for i, row in enumerate(records, start=2):  # Skip header
            if row.get("Username", "").lower() == username.lower():
                print(f"Found existing user at row {i}, updating points...")
                current_points = row.get("Points", 0)
                worksheet.update_cell(i, 2, current_points + 1)  # Column B = Points
                worksheet.update_cell(i, 3, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                print(f"Updated {username} to {current_points + 1} points")
                return True
        
        # New user
        print(f"Adding new user {username}...")
        worksheet.append_row([
            username, 
            1, 
            datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ])
        print(f"Added new user {username} with 1 point")
        return True
        
    except gspread.exceptions.APIError as e:
        print(f"Google Sheets API Error: {e}")
    except Exception as e:
        print(f"Unexpected error in update_points: {type(e).__name__}: {str(e)}")
        import traceback
        traceback.print_exc()
    
    return False

# Initialize in on_ready()
@bot.event
async def on_ready():
    print(f"[✅] logged in as {bot.user}")
    
    # Initialize Google Sheets
    bot.sheets = GoogleSheetsLogger()
    if not bot.sheets.client:
        print("⚠️ Warning: Google Sheets not initialized properly")
    else:
        print("✅ Google Sheets initialized successfully")
        
    # Initialize reaction logger
    await bot.reaction_logger.on_ready_setup()
    
    # Register commands
    from roblox_commands import create_sc_command
    create_sc_command(bot)
    
    # Sync commands
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
    except Exception as e:
        print(f"Command sync error: {e}")
        

# --- Slash Commands ---
@bot.tree.command(name="commands", description="List all available commands")
@has_allowed_role()
async def command_list(interaction: discord.Interaction):
    """Display help menu with all commands"""
    embed = discord.Embed(
        title="📜 Available Commands",
        color=discord.Color.blue()
    )
    
    categories = {
        "🔍 Reaction Monitoring": [
            "/reaction-setup - Setup reaction logger",
            "/reaction-add - Add channels to monitor",
            "/reaction-remove - Remove monitored channels",
            "/reaction-list - List monitored channels"
        ],
        "🛠️ Utility": [
            "/ping - Check bot responsiveness",
            "/commands - Show this help message"
        ],
        "🎮 Roblox Tools": [
            "/sc - Security Check Roblox user"
        ]
    }
    
    for name, value in categories.items():
        embed.add_field(name=name, value="\n".join(value), inline=False)
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="ping", description="Check bot latency")
@has_allowed_role()
async def ping(interaction: discord.Interaction):
    """Check bot responsiveness"""
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(
        f"🏓 Pong! Latency: {latency}ms",
        ephemeral=True
    )

@bot.tree.command(name="reaction-setup", description="Setup reaction monitoring")
@min_rank_required(Config.MONITOR_ROLE_ID)
async def reaction_setup(
    interaction: discord.Interaction,
    log_channel: discord.TextChannel,
    monitor_channels: str
):
    await bot.reaction_logger.setup(interaction, log_channel, monitor_channels)

@bot.tree.command(name="reaction-add", description="Add channels to monitor")
@min_rank_required(Config.MONITOR_ROLE_ID)
async def reaction_add(
    interaction: discord.Interaction,
    channels: str
):
    await bot.reaction_logger.add_channels(interaction, channels)

@bot.tree.command(name="reaction-remove", description="Remove channels from monitoring")
@min_rank_required(Config.MONITOR_ROLE_ID)
async def reaction_remove(
    interaction: discord.Interaction,
    channels: str
):
    await bot.reaction_logger.remove_channels(interaction, channels)

@bot.tree.command(name="reaction-list", description="List monitored channels")
@min_rank_required(Config.MONITOR_ROLE_ID)
async def reaction_list(interaction: discord.Interaction):
    await bot.reaction_logger.list_channels(interaction)

# --- Event Handlers ---
@bot.event
async def on_ready():
    print(f"[✅] logged in as {bot.user}")

    # Initializes reaction logger default channels
    await bot.reaction_logger.on_ready_setup()
    
    if not hasattr(bot, 'rate_limiter'):
        bot.rate_limiter = RateLimiter(calls_per_minute=GLOBAL_RATE_LIMIT)
    
    try:
        # Create SC command with rate limiting
        from roblox_commands import create_sc_command
        create_sc_command(bot) 
    
    
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} commands")
        print("Commands:", [cmd.name for cmd in bot.tree.get_commands()])
    except Exception as e:
        print(f"Command sync error: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    """Handle members leaving with deserter role"""
    guild = member.guild
    if not (deserter_role := guild.get_role(Config.DESERTER_ROLE_ID)):
        return
        
    if deserter_role not in member.roles:
        return
        
    if not (alert_channel := guild.get_channel(Config.DESERTER_ALERT_CHANNEL_ID)):
        return
        
    embed = discord.Embed(
        title="🚨 Deserter Alert",
        description=f"{member.mention} with role the {deserter_role.mention} left the server!",
        color=discord.Color.red()
    )
    embed.set_thumbnail(url=member.display_avatar.url)
    
    await alert_channel.send(
        content=f"<@&{Config.HIGH_COMMAND_ROLE_ID}>",
        embed=embed
    )

@bot.event 
async def on_member_update(before: discord.Member, after: discord.Member):
    """Send welcome message when RMP role is added"""
    if not (rmp_role := after.guild.get_role(Config.RMP_ROLE_ID)):
        return
        
    if rmp_role in before.roles or rmp_role not in after.roles:
        return
        
    embed = discord.Embed(
        title="Welcome to the Royal Military Police",
        description="**1.** Make sure to read all of the rules found in <#1165368313925353580>\n\n"
                   "**2.** You can NOT enforce the MSL (Manual of Service Law).\n\n"
                   "**3.** You can't use your L85 unless you are doing it for Self-Militia. (Self-defence)\n\n"
                   "**4.** Make sure to follow the Chain Of Command. Inspector > Chief Inspector > Superintendent > Major > Lieutenant Colonel > Colonel > Commander > Provost Marshal\n\n"
                   "**5.** For phases, you may wait for one to be hosted in <#1207367013698240584> or request the phase you need in <#1270700562433839135>.\n\n"
                   "**6.** All the information about the Defence School of Policing and Guarding is found in both <#1237062439720452157> and <#1207366893631967262>\n\n"
                   "**7.** Choose your timezone here https://discord.com/channels/1165368311085809717/1165368313925353578\n\n"
                   "**8.** You will be ranked Private but if you ever decide to leave RMP you will get your original rank back.\n\n"
                   "**Besides that, good luck with your phases!**",
        color=discord.Color.red()
    )
    
    try:
        await after.send(embed=embed)
    except discord.Forbidden:
        if welcome_channel := after.guild.get_channel(722002957738180620):
            await welcome_channel.send(f"{after.mention}", embed=embed)
    except discord.HTTPException as e:
        print(f"Failed to send welcome message: {e}")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Handle reaction events"""
    await bot.reaction_logger.log_reaction(payload)

# --- Flask Setup ---
app = Flask(__name__)
keep_alive = True

@app.route('/')
def home():
    return "Bot is running", 200

@app.route('/shutdown', methods=['POST'])
def shutdown():
    global keep_alive
    keep_alive = False
    return "Shutting down...", 200

def run_flask():
    """Run Flask in a background thread"""
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

async def run_bot():
    while True:
        try:
            await bot.start(TOKEN)
        except discord.errors.HTTPException as e:
            if e.status == 429:
                retry_after = e.response.headers.get('Retry-After', 30)
                print(f"Rate limited during login. Waiting {retry_after} seconds...")
                await asyncio.sleep(float(retry_after))
                continue
            raise
        except Exception as e:
            print(f"Unexpected error: {e}")
            break
        else:
            break

if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    asyncio.run(run_bot())
