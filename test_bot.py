import asyncio
import os
import asyncpg
from aiogram import Bot
from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv()


async def check_system():
    print("\n🔍 НАЧИНАЕМ ПРОВЕРКУ ВСЕХ СИСТЕМ...\n")

    # 1. Проверка Telegram токена
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("❌ ОШИБКА: TELEGRAM_BOT_TOKEN отсутствует в файле .env")
        return

    bot = Bot(token=token)
    try:
        me = await bot.get_me()
        print(f"✅ [1/3] Telegram подключен! Бот: @{me.username} (ID: {me.id})")
        print(f"👉 Убедитесь, что вы пишете именно пользователю: https://t.me/{me.username}")
    except Exception as e:
        print(f"❌ [1/3] Ошибка подключения к Telegram: {e}")
        await bot.session.close()
        return

    # 2. Проверка базы данных PostgreSQL
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ ОШИБКА: DATABASE_URL отсутствует в файле .env")
    else:
        try:
            conn = await asyncpg.connect(db_url)
            count = await conn.fetchval("SELECT COUNT(*) FROM movies")
            print(f"✅ [2/3] База данных в порядке! В таблице movies сейчас фильмов: {count}")
            await conn.close()
        except Exception as e:
            print(f"❌ [2/3] Ошибка базы данных: {e}")

    # 3. Проверка нейросети Gemini
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        print("❌ ОШИБКА: GEMINI_API_KEY отсутствует в файле .env")
    else:
        proxy = os.getenv("GEMINI_PROXY") or os.getenv("HTTPS_PROXY")
        http_opts = types.HttpOptions(timeout=15_000)
        if proxy:
            http_opts.client_args = {"proxy": proxy}
            http_opts.async_client_args = {"proxy": proxy}

        client = genai.Client(api_key=api_key, http_options=http_opts)
        model = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents="Ответь одним словом: тест",
            )
            print(f"✅ [3/3] Gemini API работает! Ответ нейросети: {(response.text or '').strip()}")
        except Exception as e:
            print(f"❌ [3/3] Ошибка Gemini API ({model}): {e}")

    await bot.session.close()
    print("\n🏁 ПРОВЕРКА ЗАВЕРШЕНА.\n")


if __name__ == "__main__":
    asyncio.run(check_system())