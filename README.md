# Telegram-бот рекомендаций

Бот подбирает 1–2 фильма, сериала или мультфильма из каталога PostgreSQL по запросу пользователя. При первом запуске создаёт таблицы и добавляет 10 стартовых записей, если таблица `movies` пуста.

## Запуск локально

1. Установите Python 3.11 или новее.
2. Скопируйте `.env.example` в `.env` и заполните токен Telegram, ключ Gemini и URL Neon DB.
3. Установите зависимости и запустите бота:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python bot.py
```

## Docker

Создайте заполненный `.env` и выполните:

```powershell
docker compose up --build -d
docker compose logs -f bot
```

Для Railway или Render используйте Dockerfile и задайте те же переменные в настройках сервиса. Приложение работает через Telegram long polling, поэтому входящий HTTP-порт не требуется.

`GEMINI_MODEL` можно изменить в окружении. По умолчанию используется `gemini-3.8-flash`.