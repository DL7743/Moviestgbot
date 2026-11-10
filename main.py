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
from google.genai import types

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# Список моделей: если 3.8 временно перегружена (503), пробуем резервные
AVAILABLE_MODELS = [
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
]
GEMINI_TIMEOUT_SECONDS = 45

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
 id SERIAL PRIMARY KEY, item_id TEXT UNIQUE, title TEXT,
 original_title TEXT, content_type TEXT, year INT,
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
        logger.warning("Каталог %s не найден", CATALOG_PATH)
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
            elif field == "year" and val is not None:
                try:
                    val = int(str(val).strip())
                except (ValueError, TypeError):
                    val = None
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
                        logger.info("Успешно загружено тайтлов в БД: %d", len(seeds))
            pool = new_pool
        except Exception:
            await new_pool.close()
            raise


async def generate_recommendation(prompt: str) -> str:
    if gemini_client is None:
        raise RuntimeError("Gemini-клиент не инициализирован")

    models = list(dict.fromkeys(AVAILABLE_MODELS))
    last_error = None

    for model in models:
        for attempt in range(2):
            try:
                logger.info("Запрос к модели %s (попытка %d)...", model, attempt + 1)
                
                config_params = {
                    "system_instruction": SYSTEM,
                    "max_output_tokens": 1024,
                }
                try:
                    config_params["thinking_config"] = types.ThinkingConfig(thinking_level="low")
                except Exception:
                    pass

                response = await asyncio.wait_for(
                    gemini_client.aio.models.generate_content(
                        model=model,
                        contents=prompt,
                        config=types.GenerateContentConfig(**config_params),
                    ),
                    timeout=GEMINI_TIMEOUT_SECONDS,
                )
                answer = (response.text or "").strip()
                if not answer:
                    raise RuntimeError("Модель вернула пустой ответ")
                return answer

            except Exception as error:
                last_error = error
                err_str = str(error)
                logger.warning("Сбой модели %s: %s", model, error)

                # При 503 (высокая нагрузка на сервера Google) ждем 2 сек и повторяем
                if any(keyword in err_str for keyword in ("503", "UNAVAILABLE", "high demand")):
                    if attempt == 0:
                        logger.info("Сервер Gemini перегружен, повтор через 2 сек...")
                        await asyncio.sleep(2)
                        continue

                break

    raise RuntimeError(f"Сбой генерации: {last_error}")


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
    logger.info("Команда /start от %s", message.from_user.id if message.from_user else "unknown")
    await message.answer(
        "Привет! Я подбираю фильмы и сериалы из каталога.\n"
        "Напиши, что хочется посмотреть (например: «триллер с неожиданным финалом» или «лёгкая комедия на вечер»)."
    )


@dp.message(F.text, ~F.text.startswith("/"))
async def recommendation_handler(message: Message) -> None:
    if not message.text:
        return

    query = message.text.strip()
    user_id = message.from_user.id if message.from_user else 0
    logger.info("Получен запрос от [%s]: %s", user_id, query)

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
        err_msg = html.escape(str(err))
        await status_msg.edit_text(
            f"⚠️ <b>Ошибка нейросети:</b>\n<code>{err_msg}</code>\n\nПопробуйте ещё раз через полминуты.",
            parse_mode="HTML",
        )
        return

    try:
        async with pool.acquire() as connection:
            await connection.execute(
                "INSERT INTO user_history (user_id, user_query, bot_response) VALUES ($1, $2, $3)",
                user_id, query, answer,
            )
    except Exception:
        pass

    try:
        await status_msg.delete()
    except Exception:
        pass

    for chunk in split_message(answer):
        await message.answer(chunk)


async def main() -> None:
    global gemini_client
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    api_key = os.getenv("GEMINI_API_KEY")
    if not token or not api_key or not os.getenv("DATABASE_URL"):
        raise RuntimeError("Заполните TELEGRAM_BOT_TOKEN, GEMINI_API_KEY и DATABASE_URL в .env")

    # Прокси только для Gemini
    proxy = os.getenv("GEMINI_PROXY") or os.getenv("HTTPS_PROXY")
    http_opts = types.HttpOptions()
    if proxy:
        logger.info("Используется прокси для Gemini: %s", proxy)
        http_opts.client_args = {"proxy": proxy}
        http_opts.async_client_args = {"proxy": proxy}

    gemini_client = genai.Client(api_key=api_key, http_options=http_opts)

    # Стандартный клиент Telegram без ошибочных параметров
    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=None))

    await bot.delete_webhook(drop_pending_updates=True)

    try:
        await initialize_database()
        logger.info("База данных готова к работе!")
    except Exception as e:
        logger.exception("Ошибка инициализации базы при старте: %s", e)

    try:
        logger.info("Бот запущен и ожидает сообщений!")
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        if gemini_client:
            await gemini_client.aio.aclose()
        if pool:
            await pool.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")