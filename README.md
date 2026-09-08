# Amazon Parser Dashboard

Streamlit-дашборд для таблицы BSR_Competitors_Tracker. Читает Google Sheets
(листы Current, History, Competitors) в режиме только для чтения — ничего
не пишет и не может изменить таблицу.

## Локальный запуск
    pip install -r requirements.txt
    streamlit run app.py

## Деплой на Streamlit Cloud
1. Запушить репозиторий на GitHub.
2. share.streamlit.io -> New app -> выбрать репозиторий, main file: app.py
3. До деплоя: Settings -> Secrets -> вставить содержимое JSON-ключа,
   разложенное по TOML (шаблон в .streamlit/secrets.toml.example).
4. Убедиться, что таблица BSR_Competitors_Tracker расшарена на client_email
   из ключа.