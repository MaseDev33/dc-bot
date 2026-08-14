# masecodes.dev Discord Bots

Two Discord bots (Main + Appeals) started from a single `main.py`. Designed for Python 3.12+, async, and SQLite persistence.

See `.env.example` for environment variables.

Run locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env
python main.py
```

Run with Docker:

```bash
docker compose build
docker compose up -d
docker compose logs -f
```

Data persistence:

Mount `./data:/app/data` so `data/bot.db` persists between container restarts.

Developer notes: see code in `bot/` for handlers, database, and background tasks.
