# Competitor BSR

Отслеживание позиций (BSR), цен и наличия товаров на Amazon: наши товары против конкурентов,
по нескольким странам, с ежедневной историей.

**Дашборд:** https://competitor-bsr.streamlit.app/

## Что делает

Каждый день по расписанию собираются данные по парам «наш товар — конкурент», складываются в
PostgreSQL и показываются в дашборде: текущее состояние, история по дням, управление списком пар
и запуск сбора вручную.

## Как это устроено

```
PostgreSQL (пары, расписание) --> парсер (ScrapingDog) --> Google Sheets --> синк --> PostgreSQL
                                                                                        |
                                                              дашборд (Streamlit) <-----+
```

- **Сбор** — GitHub Actions, workflow `.github/workflows/collect.yml`. Запускается внешним
  «будильником» (cron-job.org) через `workflow_dispatch`, потому что собственное расписание
  GitHub срабатывает нерегулярно.
- **Защита от лишних трат** — перед каждым сбором `should_collect_now.py` проверяет, что время
  наступило, сегодня ещё не собирали и нет незавершённой попытки. Проверка «падает закрыто»:
  при любой ошибке сбор не начинается.
- **Дашборд** — `app.py` (точка входа Streamlit Cloud) вызывает `dashboard_db.py`, который
  читает PostgreSQL. Схема в базе называется `bsr_radar`.
- **Google Sheets** остаётся промежуточным местом записи результатов; пары и расписание ведутся
  только в базе.

## Файлы, с которых стоит начать

| Файл | Зачем |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Подробная карта: этапы, решения, открытые вопросы |
| [dashboard_db.py](dashboard_db.py) | Весь дашборд |
| [parser_not_test.py](parser_not_test.py) | Сбор данных |
| [sync_sheets_to_db.py](sync_sheets_to_db.py) | Перенос результатов из Sheets в PostgreSQL |
| [db_runs.py](db_runs.py) | Допуск к платному сбору |
| [schema.sql](schema.sql) + [migrations/](migrations/) | Структура базы |

## Запуск локально

```bash
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m streamlit run app.py
```

Нужен файл `.env` с `DATABASE_URL` и ключ Google service account. Секреты в репозиторий не
попадают — см. `.gitignore` и `.streamlit/secrets.toml.example`.

## Тесты

```bash
.venv\Scripts\python -m pytest -q
```

SQL и защиту сбора можно дополнительно прогнать на настоящем временном PostgreSQL:
[tools/verify_on_temporary_postgres.py](tools/verify_on_temporary_postgres.py) — боевая база при
этом не используется.
