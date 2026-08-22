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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
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
        self.ACTIVITY_CHANNEL_ID = os.getenv('DISCORD_ACTIVITY_CHANNEL_ID')
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
        
        try:
            response = requests.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            
            if data and "data" in data and len(data["data"]) > 0:
                return {"success": True, "data": data["data"][0]["raw_text"]}
            return {"success": False, "error": "No data available"}
            
        except requests.RequestException as e:
            logger.error(f"Failed to fetch {data_type} for {airport_code}: {e}")
            return {"success": False, "error": str(e)}

class VatsimClient:
    # Stay under 10 VATSIM API v2 requests per minute
    V2_MAX_REQUESTS_PER_MINUTE = 9
    V2_WINDOW_SECONDS = 60.0

    def __init__(self):
        self.api_url = "https://data.vatsim.net/v3/vatsim-data.json"
        self.atc_history_url = "https://api.vatsim.net/v2/members/{cid}/atc"
        self._v2_request_times = []
        self._v2_lock = asyncio.Lock()
        
    async def get_controllers(self) -> list:
        try:
            response = requests.get(self.api_url)
            response.raise_for_status()  # This will raise an exception for bad status codes
            
            # Check if response content is empty
            if not response.content:
                logger.error("Empty response received from VATSIM API")
                return []
            
            # Try to parse JSON and handle potential parsing errors
            try:
                data = response.json()
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse VATSIM API response: {e}")
                logger.debug(f"Raw response: {response.text[:200]}...")  # Log first 200 chars of response
                return []
            
            # Check if controllers key exists
            controllers = data.get("controllers")
            if controllers is None:
                logger.error("No 'controllers' key in VATSIM API response")
                return []
                
            return controllers
            
        except requests.RequestException as e:
            logger.error(f"Failed to fetch VATSIM data: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error in get_controllers: {e}")
            return []

    @staticmethod
    def parse_session_time(value: str):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            logger.error(f"Failed to parse session timestamp: {value}")
            return None

    async def _wait_for_v2_slot(self):
        """Block until a VATSIM API v2 request is allowed (< 10 per minute)."""
        while True:
            async with self._v2_lock:
                now = time.monotonic()
                cutoff = now - self.V2_WINDOW_SECONDS
                self._v2_request_times = [t for t in self._v2_request_times if t > cutoff]
                if len(self._v2_request_times) < self.V2_MAX_REQUESTS_PER_MINUTE:
                    self._v2_request_times.append(now)
                    return
                wait = self.V2_WINDOW_SECONDS - (now - self._v2_request_times[0]) + 0.05
            logger.info(f"VATSIM API v2 rate limit reached, waiting {wait:.1f}s")
            await asyncio.sleep(wait)

    async def get_atc_sessions_since(self, cid: str, since: datetime) -> list:
        """Fetch ATC sessions for a CID until sessions are older than `since`."""
        sessions = []
        limit = 100
        offset = 0
        since_utc = since.astimezone(timezone.utc)

        while True:
            url = self.atc_history_url.format(cid=cid)
            params = {"limit": limit, "offset": offset}
            await self._wait_for_v2_slot()

            def _get():
                return requests.get(url, params=params, timeout=30)

            try:
                response = await asyncio.to_thread(_get)
            except requests.RequestException as e:
                logger.error(f"Failed to fetch ATC history for CID {cid}: {e}")
                break

            if response.status_code == 429:
                retry_after = int(response.headers.get("Retry-After", "60"))
                logger.warning(f"VATSIM ATC API rate limited for CID {cid}, waiting {retry_after}s")
                await asyncio.sleep(retry_after)
                continue

            if response.status_code == 404:
                logger.debug(f"No VATSIM ATC history for CID {cid}")
                break

            if response.status_code != 200:
                logger.warning(f"VATSIM ATC API {response.status_code} for CID {cid}")
                break

            try:
                payload = response.json()
            except json.JSONDecodeError as e:
                logger.error(f"Failed to parse ATC history for CID {cid}: {e}")
                break

            items = payload.get("items") or []
            if not items:
                break

            reached_end = False
            for item in items:
                connection = item.get("connection_id") or {}
                start = self.parse_session_time(connection.get("start"))
                if start and start < since_utc:
                    reached_end = True
                    break
                sessions.append(item)

            if reached_end or len(items) < limit:
                break

            offset += limit

        return sessions

class RosterClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.api_url = "https://core.vateud.net/api/facility/roster"
        self.logger = logging.getLogger()
        self._session = curl_requests.Session(impersonate="chrome131")
        
    async def get_roster(self) -> dict:
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
            
            self.logger.info(f"Response Status: {response.status_code}")
            
            if response.status_code != 200:
                if "Just a moment" in response.text:
                    self.logger.error(
                        "VATEUD API blocked by Cloudflare (403). "
                        "Update curl_cffi or contact VATEUD to whitelist this server."
                    )
                else:
                    self.logger.error(f"API Response Error: {response.text[:500]}")
                raise Exception(f"API request failed with status {response.status_code}")
                
            data = response.json()
            if not data.get('success'):
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
            return None

class VATTurkBot(commands.Bot):
    def __init__(self, config):
        self.CONTROLLER_ROLE_ID = 1326978814131048489  # "Online ATC" role
        self.role_error_logged = {}
        self.startup_complete = False
        
        # Update intents to include guild members
        intents = discord.Intents.default()
        intents.message_content = True
        intents.guilds = True
        intents.guild_messages = True
        intents.members = True  # Need this to access member nicknames
        
        super().__init__(command_prefix="!", intents=intents)
        
        self.config = config
        self.weather_api = WeatherAPI(config.CHECKWX_API_KEY)
        self.vatsim_client = VatsimClient()
        self.roster_client = RosterClient(config.VATEUD_API_KEY)
        self.telegram_bot = Bot(token=config.TELEGRAM_TOKEN)
        
        # Initialize tracking variables
        self.callsigns = self.load_callsigns('callsigns.txt')
        self.callsign_set = {cs.upper() for cs in self.callsigns}
        self.callsign_status = {callsign: None for callsign in self.callsigns}
        self.trvac_roster = set()
        self.trvac_controllers = set()
        self.last_roster_update = None
        self.roster_update_task = None
        self.first_check = True
        self.activity_timezone = ZoneInfo("Europe/Istanbul")
        self.activity_min_hours = 5.0
        self.activity_window_months = 6
        self.activity_state_file = Path("activity_check_state.json")
        self.activity_check_running = False
        
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
                    logger.info(f"HTTP Request: POST https://api.telegram.org/bot{self.config.TELEGRAM_TOKEN}/sendMessage \"HTTP/1.1 200 OK\"")
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

    async def update_roster(self):
        try:
            logger.info("Starting roster update...")
            roster_data = await self.roster_client.get_roster()
            
            if not roster_data:
                logger.error("Failed to fetch roster data")
                return False
                
            staff_cids = set()
            for staff_group in roster_data.get('staff', []):
                if isinstance(staff_group, list):
                    staff_cids.update(str(member['cid']) for member in staff_group if 'cid' in member)
                elif 'cid' in staff_group:
                    staff_cids.add(str(staff_group['cid']))
            
            controller_cids = {str(cid) for cid in roster_data.get('controllers', [])}
            self.trvac_controllers = controller_cids
            self.trvac_roster = staff_cids | controller_cids
            self.last_roster_update = asyncio.get_event_loop().time()
            
            logger.info(
                f"Roster update completed: {len(self.trvac_roster)} total members "
                f"({len(self.trvac_controllers)} controllers)"
            )
            return True
            
        except Exception as e:
            logger.error(f"Error updating roster: {e}")
            return False

    async def schedule_roster_updates(self):
        """Schedule periodic roster updates"""
        while True:
            try:
                await self.update_roster()
                # Wait for 1 hour before next update
                await asyncio.sleep(3600)  # 3600 seconds = 1 hour
            except Exception as e:
                logger.error(f"Error in roster update schedule: {e}")
                await asyncio.sleep(60)  # Wait 1 minute before retrying if there's an error

    def _subtract_months(self, dt: datetime, months: int) -> datetime:
        year = dt.year
        month = dt.month - months
        while month <= 0:
            month += 12
            year -= 1
        return dt.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)

    def _load_activity_state(self) -> dict:
        try:
            if self.activity_state_file.exists():
                return json.loads(self.activity_state_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.error(f"Failed to load activity check state: {e}")
        return {}

    def _save_activity_state(self, state: dict):
        try:
            self.activity_state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as e:
            logger.error(f"Failed to save activity check state: {e}")

    def _activity_hours_from_session(self, session: dict, now_utc: datetime) -> tuple:
        connection = session.get("connection_id") or {}
        callsign = str(connection.get("callsign") or "").strip().upper()
        start = VatsimClient.parse_session_time(connection.get("start"))
        end = VatsimClient.parse_session_time(connection.get("end")) or now_utc
        if not start:
            return callsign, 0.0, None
        hours = max((end - start).total_seconds(), 0) / 3600
        return callsign, hours, start

    async def get_activity_channel(self):
        channel_id = self.config.ACTIVITY_CHANNEL_ID
        if not channel_id:
            logger.error("DISCORD_ACTIVITY_CHANNEL_ID is not set")
            return None
        channel = self.get_channel(int(channel_id))
        if channel:
            return channel
        try:
            return await self.fetch_channel(int(channel_id))
        except Exception as e:
            logger.error(f"Failed to resolve activity announcement channel {channel_id}: {e}")
            return None

    async def send_activity_announcement(self, message: str, channel=None):
        if channel is None:
            channel = await self.get_activity_channel()
        if not channel:
            logger.error(f"Activity announcement not sent (no channel): {message}")
            return
        try:
            await channel.send(message)
        except Exception as e:
            logger.error(f"Failed to send activity announcement: {e}")

    async def run_activity_check(self, force: bool = False):
        if self.activity_check_running:
            logger.info("Activity check already in progress, skipping")
            return
        self.activity_check_running = True
        channel = None
        started = False
        try:
            now_local = datetime.now(self.activity_timezone)
            month_key = now_local.strftime("%Y-%m")
            state = self._load_activity_state()
            if not force and state.get("last_run_month") == month_key:
                logger.info(f"Activity check already completed for {month_key}")
                return

            channel = await self.get_activity_channel()
            if not channel:
                logger.error("Cannot start activity check without DISCORD_ACTIVITY_CHANNEL_ID")
                return

            logger.info("Starting monthly controller activity check...")
            self.callsigns = self.load_callsigns("callsigns.txt")
            self.callsign_set = {cs.upper() for cs in self.callsigns}
            if not self.callsign_set:
                logger.error("Activity check failed: callsigns.txt is empty or could not be read")
                return
            roster_ok = await self.update_roster()
            if not roster_ok:
                logger.error("Activity check failed: could not fetch the VATEUD roster")
                return

            controller_cids = sorted(self.trvac_controllers, key=lambda cid: int(cid) if cid.isdigit() else cid)
            if not controller_cids:
                logger.error("Activity check failed: VATEUD roster returned no controller CIDs")
                return

            await self.send_activity_announcement("Activity check started.", channel)
            started = True

            window_end = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
            window_start = self._subtract_months(window_end, self.activity_window_months)
            now_utc = datetime.now(timezone.utc)
            window_start_utc = window_start.astimezone(timezone.utc)
            window_end_utc = window_end.astimezone(timezone.utc)

            failed_count = 0
            for index, cid in enumerate(controller_cids, start=1):
                sessions = await self.vatsim_client.get_atc_sessions_since(cid, window_start)
                six_month_hours = 0.0
                for session in sessions:
                    callsign, hours, start = self._activity_hours_from_session(session, now_utc)
                    if not callsign or callsign not in self.callsign_set or not start:
                        continue
                    if window_start_utc <= start < window_end_utc:
                        six_month_hours += hours

                six_month_hours = round(six_month_hours, 2)
                if six_month_hours < self.activity_min_hours:
                    failed_count += 1
                    await self.send_activity_announcement(
                        f"CID {cid} activity failed. User connected {six_month_hours:.2f} hours "
                        f"in 6 months, which is below required.",
                        channel,
                    )

                if index % 10 == 0 or index == len(controller_cids):
                    logger.info(f"Activity check progress: {index}/{len(controller_cids)} controllers")

            self._save_activity_state({
                "last_run": now_local.isoformat(),
                "last_run_month": month_key,
                "checked": len(controller_cids),
                "below_requirement": failed_count,
            })
            logger.info(
                f"Activity check complete: {len(controller_cids)} controllers, "
                f"{failed_count} below {self.activity_min_hours}h"
            )
        except Exception as e:
            logger.error(f"Activity check failed: {e}", exc_info=True)
        finally:
            if started:
                await self.send_activity_announcement("Activity check finished.", channel)
            self.activity_check_running = False

    @tasks.loop(hours=1)
    async def monthly_activity_check_loop(self):
        now_local = datetime.now(self.activity_timezone)
        if now_local.day != 1:
            return
        await self.run_activity_check(force=False)

    @monthly_activity_check_loop.before_loop
    async def before_monthly_activity_check_loop(self):
        await self.wait_until_ready()

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
                    if cid not in self.trvac_roster:
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

        @self.tree.command(
            name="activitycheck",
            description="Run the monthly vACC activity check now (owner only)",
            guild=discord.Object(id=self.config.GUILD_ID),
        )
        async def activitycheck(interaction: discord.Interaction):
            if interaction.user.id != self.config.OWNER_ID:
                await interaction.response.send_message("Permission denied", ephemeral=True)
                return
            if self.activity_check_running:
                await interaction.response.send_message("An activity check is already running.", ephemeral=True)
                return
            await interaction.response.send_message("Starting roster activity check. This may take several minutes.")
            await self.run_activity_check(force=True)

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
        self.check_vatsim.start()
        self.monthly_activity_check_loop.start()
        
        # Create task for roster updates
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