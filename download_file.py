from aiogram import Bot
import asyncio

BOT_TOKEN = "8503677714:AAGkEszS_ZKk4KcavxUFmd_iv3tzGVbX4Co"
bot = Bot(token=BOT_TOKEN)

async def download_file(file_id, file_name):
    # передаём file_id как строку
    file = await bot.get_file(file_id)
    await bot.download_file(file.file_path, destination=file_name)
    print(f"Файл {file_name} сохранён локально")

# пример вызова
file_id_from_db = "BQACAgIAAxkBAAIBMGluCtiNjhe6y_tPh0azwl3jLWEAAweOAAJ4O3BLjEKfqRi6bGg4BA"
asyncio.run(download_file(file_id_from_db, "my_photo.jpg"))
