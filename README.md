# Beauty Manager Bot

Первый рабочий каркас Telegram-бота для канала «Косметика | Парфюм | Москва».

## Переменная окружения

`TELEGRAM_BOT_TOKEN` — токен BotFather. Никогда не добавляй его в GitHub-код.

## Render

Build command:
`pip install -r requirements.txt`

Start command:
`uvicorn main:app --host 0.0.0.0 --port $PORT`

## Следующий этап

Подключить OpenAI API, базу товаров, хранение фотографий, черновики, кнопки «Одобрить/Изменить/Отклонить» и публикацию в канал.
