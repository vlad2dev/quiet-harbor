# Build: docker build -t my-bot .  The match mounts the map at /arena.
FROM python@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /bot
COPY mechfight.py bot.py /bot/
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
