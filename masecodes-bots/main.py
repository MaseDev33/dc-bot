import os
import asyncio
import logging
import signal
from dotenv import load_dotenv

from bot.database import Database
from bot.main_bot import create_main_bot
from bot.appeals_bot import create_appeals_bot


def configure_logging():
    log_dir = os.path.join(os.getcwd(), "logs")
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    fh = logging.FileHandler(os.path.join(log_dir, "bot.log"))
    fh.setLevel(logging.INFO)
    logging.getLogger().addHandler(fh)


async def main():
    load_dotenv()
    configure_logging()
    logger = logging.getLogger("masecodes")

    db_path = os.getenv("DATABASE_PATH", "data/bot.db")
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    db = Database(db_path)
    await db.connect()

    guild_id = os.getenv("GUILD_ID")
    guild = int(guild_id) if guild_id else None

    main_bot = create_main_bot(db, guild)
    appeals_bot = create_appeals_bot(db, None)

    # start both bots concurrently
    bot_tasks = [asyncio.create_task(main_bot.start(os.getenv("MAIN_BOT_TOKEN"))), asyncio.create_task(appeals_bot.start(os.getenv("APPEALS_BOT_TOKEN")))]

    loop = asyncio.get_running_loop()
    stop = asyncio.Future()

    def _on_signal():
        if not stop.done():
            stop.set_result(True)

    try:
        loop.add_signal_handler(signal.SIGINT, _on_signal)
        loop.add_signal_handler(signal.SIGTERM, _on_signal)
    except NotImplementedError:
        # Windows or unsupported
        pass

    try:
        await stop
    finally:
        for t in bot_tasks:
            t.cancel()
        await main_bot.close()
        await appeals_bot.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
