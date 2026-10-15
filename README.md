# SectorBot
Sector Bot is a Discord bot that monitors VATSIM controller activity, assigns an “Online ATC” role, sends alerts to Discord and Telegram, and provides slash commands for METAR, TAF, and status. This project has coded on Ubuntu 24.04 LTS Noble and not tested on another enviroment.

## Installation
Create `vattur.env` in the project directory and do not commit it. It needs `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_CHANNEL_ID`, `DISCORD_OWNER_ID`, `TELEGRAM_TOKEN`, `TELEGRAM_CHANNEL_ID`, `CHECKWX_API_KEY`, and `VATEUD_API_KEY`.
