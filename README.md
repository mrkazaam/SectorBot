# SectorBot
a Discord bot that follows sector activity over VATSIM

### Enviroment

```sh
[Unit]
Description=AS_DESIRED
After=network.target

[Service]
User=your_linux_instance_username
WorkingDirectory=/path/to/your/bot
ExecStart=/usr/bin/python3 /path/to/your/bot/bot.py
Restart=always
Environment=DISCORD_BOT_TOKEN=TOKEN=YOUR_DISCORD_BOT_TOKEN
Environment=DISCORD_GUILD_ID=GUILD_ID=YOUR_DISCORD_GUILD_ID
Environment=DISCORD_CHANNEL_ID=CHANNEL_ID=YOUR_DISCORD_CHANNEL_ID
Environment=CHECKWX_API_KEY=CHECKWX_API_KEY=YOUR_CHECKWX_API_KEY
Environment=DISCORD_OWNER_ID=BOT_OWNER_DISCORD_ID
Environment=TELEGRAM_TOKEN=TELEGRAM_CHANNEL_TOKEN
Environment=TELEGRAM_CHANNEL_ID=TELEGRAM_CHANNEL_ID
Environment=VATEUD_API_KEY=VATEUD_API_KEY_FOR_ROSTER_CHECK

[Install]
WantedBy=multi-user.target
