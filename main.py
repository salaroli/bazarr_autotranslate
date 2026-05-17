import asyncio
import logging
import os
import signal
import sys
import threading
from logging.handlers import TimedRotatingFileHandler

from client import BazarrClient
from config import Config
from cooldown import CooldownCache
from models import SubtitleTranslate, SearchTask, MigrationTask
from scheduler import Orchestrator
from unique_queue import UniqueQueue
from workers import TranslationWorker, SearchWorker, MigrationWorker


def setup_logging(config: Config) -> None:
    os.makedirs(config.log_directory, exist_ok=True)
    logger = logging.getLogger("bazarr_lingarr")
    fmt = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

    file_handler = TimedRotatingFileHandler(
        os.path.join(config.log_directory, "bazarr_lingarr_autotranslate.log"),
        when="midnight", interval=1, backupCount=4,
    )
    file_handler.setFormatter(fmt)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)
    logger.setLevel(logging.DEBUG if config.log_level.upper() == "DEBUG" else logging.INFO)


async def run(config: Config) -> None:
    logger = logging.getLogger("bazarr_lingarr")
    cooldown = CooldownCache(config.action_cooldown_seconds)
    lingarr_semaphore = threading.Semaphore(1)
    whisper_semaphore = threading.Semaphore(1)

    translation_queue = UniqueQueue(key_fn=lambda x: x.queue_key)
    search_queue = UniqueQueue(key_fn=lambda x: x.queue_key)
    migration_queue = UniqueQueue(key_fn=lambda x: x.queue_key)

    async with BazarrClient(config) as client:
        if not await client.ping():
            logger.error("Cannot connect to Bazarr — check BAZARR_BASE_URL and BAZARR_API_KEY. Exiting.")
            return

        for i in range(config.num_workers):
            TranslationWorker(i, config, translation_queue, lingarr_semaphore).start()
            SearchWorker(i, config, search_queue, translation_queue, whisper_semaphore, cooldown).start()
            MigrationWorker(i, config, migration_queue).start()
        logger.info(f"Started {config.num_workers} worker(s) of each type. Entering scan loop.")

        await Orchestrator(config, client, search_queue, migration_queue, cooldown).run()


if __name__ == "__main__":
    config = Config.from_env()
    try:
        config.validate()
    except ValueError as e:
        print(e)
        sys.exit(1)

    setup_logging(config)

    logger = logging.getLogger("bazarr_lingarr")
    logger.info("=" * 55)
    logger.info("Bazarr Auto-Translate starting")
    logger.info(f"  Bazarr URL    : {config.bazarr_base_url}")
    logger.info(f"  Base langs    : {', '.join(config.base_languages) or '(none)'}")
    logger.info(f"  Target langs  : {', '.join(config.to_languages) or '(none)'}")
    logger.info(f"  Min score     : {config.min_score}")
    logger.info(f"  Workers       : {config.num_workers}")
    logger.info(f"  Scan interval : {config.interval_between_scans}s")
    logger.info(f"  Cooldown      : {config.action_cooldown_seconds}s")
    logger.info(f"  Series scan   : {config.series_scan}")
    logger.info(f"  Movies scan   : {config.movies_scan}")
    if config.source_profile_id:
        logger.info(f"  Migration     : profile {config.source_profile_id} → {config.target_profile_id}")
    logger.info("=" * 55)

    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGINT, lambda: sys.exit(0))
    loop.add_signal_handler(signal.SIGTERM, lambda: sys.exit(0))
    loop.run_until_complete(run(config))
