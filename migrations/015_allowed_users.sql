-- Допущенные для Scorecard: знаменатель процента «сколько допущенных зашли за неделю».
--
-- Вход на сайт этот список НЕ ограничивает (решение владельца 05.10.2026): дашборд по-прежнему
-- открыт всему домену maximumstores.online. Список ведёт админ во вкладке «📒 Журнал».
-- Аддитивная миграция: только создаёт новую таблицу, существующие данные не трогает.
-- Повторный запуск безопасен. Пока миграция не применена, карточки Scorecard просто нет.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS bsr_radar.allowed_users (
    email TEXT PRIMARY KEY CHECK (email = lower(email)),
    added_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
