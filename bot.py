import asyncio
import html
import json
import logging
import os
from pathlib import Path

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction
from aiogram.filters import CommandStart
from aiogram.types import Message
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Реальные и рабочие модели Google Gemini
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT_SECONDS = 15  # Не ждем дольше 15 секунд

SYSTEM = """Ты — умный киногид. Строго на основе JSON-каталога выбери 1–2 подходящих тайтла под запрос. 
Ничего не выдумывай и рекомендуй только произведения из каталога. 
Для каждого укажи название и год, прямую ссылку из каталога, описание сюжета без спойлеров в 2–3 предложениях 
и почему произведение подходит (по вайбу и тегам). Если подходящего варианта нет, честно скажи об этом. 
Пиши по-русски, простым текстом без Markdown."""

CATALOG_PATH = Path(__file__).with_name("catalog.json")
SEED_FIELDS = (
    "item_id", "title", "original_title", "content_type", "year", "genres",
    "url", "plot", "tags", "vibe", "target_audience",
)

MOVIES_SQL = """CREATE TABLE IF NOT EXISTS movies (
 id SERIAL PRIMARY KEY, item_id VARCHAR(50) UNIQUE, title VARCHAR(255),
 original_title VARCHAR(255), content_type VARCHAR(50), year INT,
 genres TEXT, url TEXT, plot TEXT, tags TEXT, vibe TEXT, target_audience TEXT
)"""
HISTORY_SQL = """CREATE TABLE IF NOT EXISTS user_history (
 id SERIAL PRIMARY KEY, user_id BIGINT, user_query TEXT,
 bot_response TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""
INSERT_SQL = """INSERT INTO movies (
 item_id, title, original_title, content_type, year, genres, url, plot, tags, vibe, target_audience
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
ON CONFLICT (item_id) DO NOTHING"""

pool: asyncpg.Pool | None = None
gemini_client: genai.Client | None = None
db_lock = asyncio.Lock()
dp = Dispatcher()


def load_seed_movies() -> list[tuple]:
    if not CATALOG_PATH.exists():
        logger.warning("Каталог %s не найден, пропускаем наполнение БД", CATALOG_PATH)
        return []

    try:
        with CATALOG_PATH.open(encoding="utf-8") as catalog_file:
            data = json.load(catalog_file)
    except Exception:
        logger.exception("Ошибка чтения %s", CATALOG_PATH)
        return []

    seeds = []
    for item in data:
        row = []
        for field in SEED_FIELDS:
            val = item.get(field)
            if isinstance(val, (list, tuple)):
                val = ", ".join(map(str, val))
            row.append(val)
        seeds.append(tuple(row))
    return seeds


async def initialize_database() -> None:
    global pool
    async with db_lock:
        if pool is not None:
            return

        database_url = os.getenv("DATABASE_URL")
        if not database_url:
            raise RuntimeError("Не задана переменная DATABASE_URL")

        new_pool = await asyncpg.create_pool(dsn=database_url, min_size=1, max_size=5, command_timeout=30)
        try:
            async with new_pool.acquire() as connection:
                await connection.execute(MOVIES_SQL)
                await connection.execute(HISTORY_SQL)
                await connection.execute(
                    "CREATE INDEX IF NOT EXISTS idx_user_history_user_created "
                    "ON user_history (user_id, created_at DESC)"
                )
                if await connection.fetchval("SELECT COUNT(*) FROM movies") == 0:
                    seeds = load_seed_movies()
                    if seeds:
                        await connection.executemany(INSERT_SQL, seeds)
                        logger.info("Добавлено стартовых тайтлов: %d", len(seeds))
            pool = new_pool
        except Exception:
            await new_pool.close()
            raise


async def generate_recommendation(prompt: str) -> str:
    if gemini_client is None:
        raise RuntimeError("Gemini-клиент не инициализирован")

    models = [MODEL]
    if FALLBACK_MODEL != MODEL:
        models.append(FALLBACK_MODEL)

    for index, model in enumerate(models):
        try:
            logger.info("Запрос к модели %s...", model)
            response = await asyncio.wait_for(
                gemini_client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=SYSTEM,
                        temperature=0.3,
                        max_output_tokens=1024,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
            answer = (response.text or "").strip()
            if not answer:
                raise RuntimeError("Модель вернула пустой ответ")
            return answer

        except (errors.APIError, TimeoutError, asyncio.TimeoutError) as error:
            logger.warning("Сбой модели %s: %s", model, error)
            if index == len(models) - 1:
                raise

    raise RuntimeError("Все модели Gemini недоступны")


def split_message(text: str, limit: int = 4000) -> list[str]:
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip()
    if text:
        chunks.append(text)
    return chunks


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Я подбираю фильмы и сериалы из каталога.\n"
        "Напиши, что хочется посмотреть (например: «триллер с неожиданным финалом» или «лёгкая комедия на вечер»)."
    )


@dp.message(F.text & ~F.text.startswith("/"))
async def recommendation_handler(message: Message) -> None:
    if not message.text:
        return

    query = message.text.strip()
    user_id = message.from_user.id if message.from_user else 0

    if pool is None:
        try:
            await initialize_database()
        except Exception:
            logger.exception("База данных недоступна")
            await message.answer("База данных временно недоступна. Попробуйте чуть позже.")
            return

    status_msg = await message.answer("Подбираю рекомендацию из каталога...")
    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        async with pool.acquire() as connection:
            # Ограничиваем до 30 тайтлов, чтобы не превысить лимиты времени и токенов
            rows = await connection.fetch(
                """SELECT item_id, title, content_type, year, genres, plot, tags, vibe, url
                   FROM movies 
                   ORDER BY id DESC 
                   LIMIT 30"""
            )
    except Exception:
        logger.exception("Ошибка чтения каталога")
        await status_msg.edit_text("Не удалось прочитать каталог. Попробуйте позже.")
        return

    if not rows:
        await status_msg.edit_text("Каталог пока пуст.")
        return

    prompt = json.dumps(
        {"user_query": query, "catalog": [dict(r) for r in rows]},
        ensure_ascii=False,
    )

    try:
        answer = await generate_recommendation(prompt)
    except Exception as err:
        logger.error("Не удалось сгенерировать рекомендацию: %s", err)
        await status_msg.edit_text("Нейросеть сейчас недоступна или перегружена. Попробуйте ещё раз через минуту.")
        return

    # Сохраняем в историю
    try:
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO user_history (user_id, user_query, bot_response) VALUES ($1, $2, $3)",
                user_id, query, answer,
            )
    except Exception:
        logger.exception("Не удалось сохранить историю")

    # Удаляем статусное сообщение и выводим ответ
    await status_msg.delete()
    for chunk in split_message(answer):
        await message.answer(chunk)


async def on_startup(bot: Bot) -> None:
    try:
        await initialize_database()
    except Exception:
        logger.exception("Не удалось подключиться к базе при старте")
    logger.info("Бот запущен!")


async def on_shutdown() -> None:
    global pool
    if pool is not None:
        await pool.close()
        pool = None
    if gemini_client is not None:
        await gemini_client.aio.aclose()


async def main() -> None:
    global gemini_client
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    api_key = os.getenv("GEMINI_API_KEY")
    if not token or not api_key or not os.getenv("DATABASE_URL"):
        raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN, GEMINI_API_KEY и DATABASE_URL в .env")

    # Настройка прокси при необходимости (например, http://127.0.0.1:10809)
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY")
    http_opts = types.HttpOptions(timeout=20_000)
    if proxy:
        logger.info("Используется прокси для Gemini: %s", proxy)
        http_opts.client_args = {"proxy": proxy}

    gemini_client = genai.Client(
        api_key=api_key,
        http_options=http_opts,
    )

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    try:
        # drop_pending_updates=True сбрасывает зависшие апдейты при старте
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (RuntimeError, KeyboardInterrupt):
        logger.info("Бот остановлен")