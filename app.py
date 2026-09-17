"""
Точка входа для Streamlit Cloud — этот конкретный деплой жёстко привязан
к файлу app.py, сменить главный файл в интерфейсе Streamlit Cloud нельзя.
Реальный код дашборда живёт в dashboard_db.py (читает Postgres, не Sheets);
здесь только запуск, чтобы существующая ссылка показывала актуальную версию.

Старая read-only версия на Google Sheets осталась в истории git.
"""

import dashboard_db

dashboard_db.main()
