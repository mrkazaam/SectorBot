import discord
from discord.ext import commands, tasks
import requests
import logging
import os
from telegram import Bot
import asyncio
from curl_cffi import requests as curl_requests
import json
from logging.handlers import TimedRotatingFileHandler
import telegram
import time

# Configure logging with rotation
def setup_logging():
    # Create formatter
    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    
    # Set up file handler with daily rotation
    file_handler = TimedRotatingFileHandler(
        'vattur.log',
        when='midnight',  # Rotate at midnight
        interval=1,       # One day per file
        backupCount=30,   # Keep last 30 days
        encoding='utf-8'
    )
    file_handler.setFormatter(formatter)
    file_handler.suffix = '%Y-%m-%d.log'  # Append date to filename
    
    # Set up console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    
    # Configure root logger
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    # httpx logs the full Telegram URL, which includes the bot token
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    
    return logger

# Initialize both loggers
logger = setup_logging()

class Config:
    def __init__(self):
        self.TOKEN = os.getenv('DISCORD_BOT_TOKEN')
        self.TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
        self.TELEGRAM_CHANNEL_ID = os.getenv('TELEGRAM_CHANNEL_ID')
        self.GUILD_ID = os.getenv('DISCORD_GUILD_ID')
        self.CHANNEL_ID = os.getenv('DISCORD_CHANNEL_ID')
        self.CHECKWX_API_KEY = os.getenv('CHECKWX_API_KEY')
        self.OWNER_ID = int(os.getenv('DISCORD_OWNER_ID'))
        self.VATEUD_API_KEY = os.getenv('VATEUD_API_KEY')
        self.ROSTER_UPDATE_INTERVAL = 3600
        
        if not self.GUILD_ID:
            raise ValueError("GUILD_ID environment variable is not set")

class WeatherAPI:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.checkwx.com"
        
    async def get_weather_data(self, airport_code: str, data_type: str) -> dict:
        url = f"{self.base_url}/{data_type}/{airport_code}/decoded"
        headers = {'X-API-Key': self.api_key}

        def _get():
            response = requests.get(url, headers=headers, timeout=(10, 20))
            response.raise_for_status()
            return response.json()
        
        try:
            data = await asyncio.to_thread(_get)
            
            if data and "data" in data and len(data["data"]) > 0:
                return {"success": True, "data": data["data"][0]["raw_text"]}
            return {"success": False, "error": "No data available"}
            
        except requests.RequestException as e:
            logger.error(f"Failed to fetch {data_type} for {airport_code}: {e}")
            return {"success": False, "error": str(e)}

class VatsimClient:
    # (connect, read). A hung socket must not stall the Discord heartbeat.
    DATA_TIMEOUT = (10, 25)
    # Both minute-loops share one download instead of fetching twice.
    DATA_CACHE_SECONDS = 25.0

    def __init__(self):
        self.api_url = "https://data.vatsim.net/v3/vatsim-data.json"
        self._data_lock = asyncio.Lock()
        self._controllers_cache = None

    def _fetch_controllers_sync(self) -> list:
        response = requests.get(self.api_url, timeout=self.DATA_TIMEOUT)
        response.raise_for_status()

        if not response.content:
            raise ValueError("Empty response received from VATSIM API")

        try:
            data = response.json()
        except json.JSONDecodeError as e:
            raise ValueError(f"Failed to parse VATSIM API response: {e}") from e

        controllers = data.get("controllers")
        if controllers is None:
            raise ValueError("No 'controllers' key in VATSIM API response")
        return controllers

    async def get_controllers(self) -> list:
        now = time.monotonic()
        cached = self._controllers_cache
        if cached is not None and now - cached[0] < self.DATA_CACHE_SECONDS:
            return cached[1]

        async with self._data_lock:
            now = time.monotonic()
            cached = self._controllers_cache
            if cached is not None and now - cached[0] < self.DATA_CACHE_SECONDS:
                return cached[1]

            try:
                controllers = await asyncio.to_thread(self._fetch_controllers_sync)
            except (requests.RequestException, ValueError) as e:
                logger.error(f"Failed to fetch VATSIM data: {e}")
                return []
            except Exception as e:
                logger.error(f"Unexpected error in get_controllers: {e}")
                return []

            self._controllers_cache = (time.monotonic(), controllers)
            return controllers

class RosterClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.api_url = "https://core.vateud.net/api/facility/roster"
        self.logger = logging.getLogger()
        self._session = curl_requests.Session(impersonate="chrome131")
        self.last_status_code = None
        self.last_error = None
        
    async def get_roster(self) -> dict:
        self.last_status_code = None
        self.last_error = None
        try:
            self.logger.info("=== VATEUD API Request ===")
            
            headers = {
                'Accept': 'application/json',
                'X-API-KEY': self.api_key,
            }
            
            self.logger.info("Making request to VATEUD API")
            
            response = await asyncio.to_thread(
                self._session.get,
                self.api_url,
                headers=headers,
                timeout=30,
            )
            
            self.last_status_code = response.status_code
            self.logger.info(f"Response Status: {response.status_code}")
            
            if response.status_code != 200:
                if response.status_code == 401:
                    self.last_error = (
                        "Invalid API Key provided. Please check VATEUD Core for your vACC's API Key"
                    )
                    self.logger.error(f"API Response Error: {self.last_error}")
                elif "Just a moment" in response.text:
                    self.last_error = (
                        "VATEUD API blocked by Cloudflare (403). "
                        "Update curl_cffi or contact VATEUD to whitelist this server."
                    )
                    self.logger.error(self.last_error)
                else:
                    self.last_error = response.text[:500]
                    self.logger.error(f"API Response Error: {self.last_error}")
                raise Exception(f"API request failed with status {response.status_code}")
                
            data = response.json()
            if not data.get('success'):
                self.last_error = str(data)
                self.logger.error(f"API Error Response: {data}")
                raise Exception("API returned error status")
            
            # Log successful response data
            self.logger.info("=== API Response Data ===")
            self.logger.info(f"Success: {data.get('success')}")
            self.logger.info(f"Staff Count: {len(data['data'].get('staff', []))}")
            self.logger.info(f"Controllers Count: {len(data['data'].get('controllers', []))}")
            self.logger.info("========================")
            
            return data['data']
            
        except Exception as e:
            self.logger.error(f"API Error: {str(e)}")
            if self.last_error is None:
                self.last_error = str(e)
            return None

class VATTurkBot(commands.Bot):
    def __init__(self, config):
        self.CONTROLLER_ROLE_ID = 1545436417822298192  # "Online ATC" role
        self.role_error_logged = {}
        self.startup_complete = False
        
        # Update intents to include guild members
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        intents.members = True  # Need this to access member nicknames
        
        # This bot never joins voice. Silence discord.py's missing-library warnings.
        from discord.voice_client import VoiceClient
        VoiceClient.warn_dave = False
        VoiceClient.warn_nacl = False

        super().__init__(command_prefix="!", intents=intents)
        
        self.config = config
        self.weather_api = WeatherAPI(config.CHECKWX_API_KEY)
        self.vatsim_client = VatsimClient()
        self.roster_client = RosterClient(config.VATEUD_API_KEY)
        self.telegram_bot = Bot(token=config.TELEGRAM_TOKEN)
        
        # Initialize tracking variables
        self.callsigns = self.load_callsigns('callsigns.txt')
        self.callsign_status = {callsign: None for callsign in self.callsigns}
        self.trvac_roster = set()
        self.roster_ready = False
        self.last_roster_update = None
        self.roster_update_task = None
        self.first_check = True
        # In-memory only: one 401 notice per unbroken failure streak (resets on restart / recovery)
        self.vateud_401_noticed = False
        
        # Register commands
        self.setup_commands()

    @staticmethod
    def load_callsigns(file_path: str) -> list:
        try:
            with open(file_path, 'r') as file:
                return [line.strip() for line in file if line.strip()]
        except Exception as e:
            logger.error(f"Failed to load callsigns: {e}")
            return []

    async def send_notification(self, message: str):
        """Send notification to Discord and Telegram with retry logic"""
        try:
            # Discord notification (keep original markdown format)
            channel = self.get_channel(int(self.config.CHANNEL_ID))
            if channel:
                await channel.send(message)
                logger.info(f"Attempting to send message to channel {channel.name}: {message}")

            # Convert Discord formatting to Telegram HTML format
            telegram_message = message.replace(":globe_with_meridians:", "🌐")  # Globe emoji
            telegram_message = telegram_message.replace(":zzz:", "💤")  # Sleeping emoji
            
            # Convert markdown bold to HTML bold
            telegram_message = telegram_message.replace("**", "<b>", 1)  # First occurrence
            telegram_message = telegram_message.replace("**", "</b>", 1)  # Second occurrence

            # Telegram notification with retry logic
            max_retries = 3
            retry_delay = 5  # seconds
            
            for attempt in range(max_retries):
                try:
                    await self.telegram_bot.send_message(
                        chat_id=self.config.TELEGRAM_CHANNEL_ID,
                        text=telegram_message,
                        parse_mode='HTML'
                    )
                    logger.info("Telegram notification sent")
                    break  # Success, exit retry loop
                    
                except telegram.error.TimedOut:
                    if attempt < max_retries - 1:  # Don't sleep on last attempt
                        logger.warning(f"Telegram notification timed out, attempt {attempt + 1}/{max_retries}. Retrying in {retry_delay} seconds...")
                        await asyncio.sleep(retry_delay)
                    else:
                        logger.error("Failed to send Telegram notification after all retries")
                        
                except telegram.error.TelegramError as e:
                    logger.error(f"Telegram error: {str(e)}")
                    break  # Don't retry other Telegram errors
                    
        except Exception as e:
            logger.error(f"Failed to send notification: {str(e)}", exc_info=True)

    async def notify_vateud_401(self):
        """Notify Discord + Telegram once per 401 failure streak (not per controller event)."""
        if self.vateud_401_noticed:
            return
        self.vateud_401_noticed = True
        detail = self.roster_client.last_error or (
            "Invalid API Key provided. Please check VATEUD Core for your vACC's API Key"
        )
        if not self.is_ready():
            await self.wait_until_ready()
        await self.send_notification(
            f"⚠️ **VATEUD API 401**\n{detail}\n"
            f"Roster updates and rogue checks are paused until `VATEUD_API_KEY` is fixed."
        )

    async def update_roster(self):
        try:
            logger.info("Starting roster update...")
            roster_data = await self.roster_client.get_roster()
            
            if not roster_data:
                logger.error("Failed to fetch roster data — rogue checks paused until roster is available")
                if self.roster_client.last_status_code == 401:
                    await self.notify_vateud_401()
                return False
                
            staff_cids = set()
            for staff_group in roster_data.get('staff', []):
                if isinstance(staff_group, list):
                    staff_cids.update(str(member['cid']) for member in staff_group if 'cid' in member)
                elif 'cid' in staff_group:
                    staff_cids.add(str(staff_group['cid']))
            
            controller_cids = {str(cid) for cid in roster_data.get('controllers', [])}
            self.trvac_roster = staff_cids | controller_cids
            self.roster_ready = True
            self.last_roster_update = asyncio.get_event_loop().time()
            # Allow a fresh notice if the key breaks again later this session
            self.vateud_401_noticed = False
            
            logger.info(
                f"Roster update completed: {len(self.trvac_roster)} total members "
                f"({len(controller_cids)} controllers)"
            )
            return True
            
        except Exception as e:
            logger.error(f"Error updating roster: {e}")
            return False

    async def schedule_roster_updates(self):
        """Load roster before online monitoring, then refresh periodically."""
        await self.wait_until_ready()
        first = True
        while True:
            try:
                ok = await self.update_roster()
                if first:
                    first = False
                    # Start VATSIM online alerts only after the first roster attempt
                    # so a startup 401 notice is not mixed into connection logs.
                    if not self.check_vatsim.is_running():
                        self.check_vatsim.start()
                await asyncio.sleep(60 if not ok else 3600)
            except Exception as e:
                logger.error(f"Error in roster update schedule: {e}")
                if first:
                    first = False
                    if not self.check_vatsim.is_running():
                        self.check_vatsim.start()
                await asyncio.sleep(60)

    @tasks.loop(minutes=1)
    async def check_vatsim(self):
        controllers = await self.vatsim_client.get_controllers()
        if not controllers:
            logger.warning("No controllers data received from VATSIM")
            return
            
        logger.debug(f"Retrieved {len(controllers)} controllers from VATSIM")
        online_callsigns = {ctrl["callsign"]: ctrl for ctrl in controllers}
        
        for callsign in self.callsigns:
            previous_status = self.callsign_status.get(callsign)
            logger.debug(f"Checking {callsign} (previous status: {previous_status})")
            
            if callsign in online_callsigns:
                controller = online_callsigns[callsign]
                name = controller.get("name", "Unknown")
                cid = str(controller.get("cid", "Unknown"))
                
                if self.first_check or previous_status != "online":
                    logger.info(f"Status change detected for {callsign}")
                    await self.notify_controller_status(callsign, name, cid, "online")
                    if not self.roster_ready:
                        logger.warning(
                            f"Skipping rogue check for {callsign} ({cid}) — VATEUD roster not loaded yet"
                        )
                    elif cid not in self.trvac_roster:
                        await self.notify_rogue_controller(callsign, name, cid)
                
                self.callsign_status[callsign] = "online"
            elif previous_status == "online":
                logger.info(f"Controller went offline: {callsign}")
                await self.notify_controller_status(callsign, "Unknown", "Unknown", "offline")
                self.callsign_status[callsign] = "offline"
        
        self.first_check = False

    async def notify_controller_status(self, callsign, name, cid, status):
        if status == "online":
            discord_msg = f":globe_with_meridians: **{callsign}** {name} - {cid} is now online."
            telegram_msg = f"🌐 {callsign} {name} - {cid} is now online."
        else:
            discord_msg = f":zzz: **{callsign}** is now offline."
            telegram_msg = f"💤 {callsign} is now offline."
        
        # Log the status change
        logger.info(discord_msg)
        
        # Send notifications
        await self.send_notification(discord_msg)

    async def notify_rogue_controller(self, callsign, name, cid):
        warning_msg = (f"⚠️ **ROGUE CONNECTION DETECTED**\n"
                      f"Controller: {callsign} ({name})\n"
                      f"CID: {cid}\n"
                      f"This controller is not in the TRvACC roster!")
        
        # Log the rogue controller detection
        logger.warning(f"Rogue controller detected: {callsign} ({cid})")
        
        # Send notifications
        await self.send_notification(warning_msg)

    def setup_commands(self):
        @self.tree.command(name="metar", description="Fetch METAR data", guild=discord.Object(id=self.config.GUILD_ID))
        async def metar(interaction: discord.Interaction, airport_code: str):
            await interaction.response.defer(ephemeral=True)
            result = await self.weather_api.get_weather_data(airport_code.upper(), "metar")
            
            if result["success"]:
                await interaction.followup.send(f"METAR for **{airport_code.upper()}**: {result['data']}")
            else:
                await interaction.followup.send(f"Failed to fetch METAR: {result['error']}")

        @self.tree.command(name="taf", description="Fetch TAF data", guild=discord.Object(id=self.config.GUILD_ID))
        async def taf(interaction: discord.Interaction, airport_code: str):
            await interaction.response.defer(ephemeral=True)
            result = await self.weather_api.get_weather_data(airport_code.upper(), "taf")
            
            if result["success"]:
                await interaction.followup.send(f"TAF for **{airport_code.upper()}**: {result['data']}")
            else:
                await interaction.followup.send(f"Failed to fetch TAF: {result['error']}")

        @self.tree.command(name="status", description="Check controller status", guild=discord.Object(id=self.config.GUILD_ID))
        async def status(interaction: discord.Interaction, callsign: str):
            await interaction.response.defer(ephemeral=True)
            status = self.callsign_status.get(callsign.upper(), "unknown")
            await interaction.followup.send(f"Controller {callsign.upper()} is currently {status}.")

        @self.tree.command(name="shutdown", description="Shut down the bot", guild=discord.Object(id=self.config.GUILD_ID))
        async def shutdown(interaction: discord.Interaction):
            if interaction.user.id == self.config.OWNER_ID:
                await interaction.response.send_message("Shutting down...")
                await self.close()
            else:
                await interaction.response.send_message("Permission denied", ephemeral=True)

    def extract_cid(self, nickname: str) -> str:
        """Extract CID from nickname formats:
        1. 'XXXXX YYYYYY - ZZZZZZZ'
        2. '|-ZZZZZZZ-|'
        """
        try:
            if not nickname:
                return None
            
            if ' - ' in nickname:
                # Format: "XXXXX YYYYYY - ZZZZZZZ"
                cid = nickname.split(' - ')[-1].strip()
                if cid.isdigit():
                    return cid
            elif '|-' in nickname and '-|' in nickname:
                # Format: "|-ZZZZZZZ-|"
                cid = nickname.replace('|-', '').replace('-|', '').strip()
                if cid.isdigit():
                    return cid
                
        except Exception as e:
            logger.error(f"Error extracting CID from nickname '{nickname}': {e}")
        return None

    @tasks.loop(minutes=1)
    async def check_controller_status(self):
        try:
            if not self.is_ready():
                await self.wait_until_ready()

            guild = self.get_guild(int(self.config.GUILD_ID))
            if not guild:
                return

            controller_role = guild.get_role(self.CONTROLLER_ROLE_ID)
            if not controller_role:
                return

            controllers = await self.vatsim_client.get_controllers()
            if controllers is None:
                return

            # Create a dict of CIDs for controllers with our callsigns
            our_online_cids = {
                str(ctrl['cid']) for ctrl in controllers 
                if ctrl['callsign'] in self.callsigns
            }
            
            logger.debug(f"Our online controllers: {len(our_online_cids)} with callsigns from our list")

            for member in guild.members:
                if not member.nick:
                    continue

                member_cid = self.extract_cid(member.nick)
                if not member_cid:
                    continue

                try:
                    # Check if member is online with one of our callsigns
                    is_online = member_cid in our_online_cids
                    has_role = controller_role in member.roles

                    if is_online and not has_role:
                        await member.add_roles(controller_role)
                        logger.info(f"Added controller role to {member.nick} (CID: {member_cid})")
                        self.role_error_logged.pop(member.id, None)
                    elif not is_online and has_role:
                        await member.remove_roles(controller_role)
                        logger.info(f"Removed controller role from {member.nick} (CID: {member_cid})")
                        self.role_error_logged.pop(member.id, None)

                except discord.Forbidden as e:
                    if member.id not in self.role_error_logged:
                        logger.error(f"Permission error for {member.nick}: {str(e)}")
                        self.role_error_logged[member.id] = True

        except Exception as e:
            logger.error(f"Error in check_controller_status: {e}", exc_info=True)

    async def setup_hook(self):
        """Verify permissions and role hierarchy during startup"""
        await self.tree.sync(guild=discord.Object(id=self.config.GUILD_ID))
        
        # Debug log to see all config variables
        logger.info("Available config variables:")
        for var in vars(self.config):
            logger.info(f"  - {var}")
        
        # Check permissions and roles once during startup
        guild = self.get_guild(int(self.config.GUILD_ID))
        if guild:
            bot_member = guild.get_member(self.user.id)
            if bot_member:
                bot_permissions = bot_member.guild_permissions
                logger.info(f"Bot permissions - Administrator: {bot_permissions.administrator}")
                logger.info(f"Bot permissions - Manage Roles: {bot_permissions.manage_roles}")
                logger.info(f"Bot's highest role: {bot_member.top_role.name} (ID: {bot_member.top_role.id}, Position: {bot_member.top_role.position})")

                controller_role = guild.get_role(self.CONTROLLER_ROLE_ID)
                if controller_role:
                    logger.info(f"Controller role: {controller_role.name} (ID: {controller_role.id}, Position: {controller_role.position})")
                    
                    if not bot_permissions.administrator and not bot_permissions.manage_roles:
                        logger.error("Bot lacks required permissions! Please grant Administrator or Manage Roles permission.")
                    if bot_member.top_role.position <= controller_role.position:
                        logger.error("Bot's role must be higher than the controller role! Please adjust role positions in server settings.")

        # Start all tasks
        logger.info("Starting bot tasks...")
        self.check_controller_status.start()
        # Roster bootstrap starts check_vatsim after the first VATEUD attempt
        self.loop.create_task(self.schedule_roster_updates())
        
        logger.info("All tasks started successfully")

    # Add these error handlers
    async def on_error(self, event_method: str, *args, **kwargs):
        logger.error(f'Error in {event_method}:', exc_info=True)

    async def on_disconnect(self):
        logger.info('Bot disconnected from Discord - Will automatically reconnect')

    async def on_connect(self):
        logger.info('Bot connected to Discord')

    async def on_resume(self):
        logger.info('Bot resumed Discord connection')

    async def on_ready(self):
        logger.info(f'Bot logged in as {self.user.name}')
        logger.info(f'Connected to guilds: {[g.name for g in self.guilds]}')

    def run(self):
        while True:
            try:
                super().run(self.config.TOKEN, reconnect=True)
            except discord.errors.ConnectionClosed:
                logger.warning("Discord connection closed - Reconnecting...")
                continue
            except Exception as e:
                logger.error(f"Fatal error occurred: {e}", exc_info=True)
                break

def main():
    config = Config()
    bot = VATTurkBot(config)
    bot.run()

if __name__ == "__main__":
    main()
