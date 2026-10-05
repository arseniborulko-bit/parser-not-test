-- Действия в дашборде по сессиям: сколько времени проводят и какие разделы открывают.
--
-- Одна строка — одно действие сотрудника на странице (клик, фильтр, переключение вкладки).
-- section заполнен, когда открыт раздел (вкладка), иначе NULL. session_id — случайный номер
-- открытой страницы, по нему считается время «от первого до последнего действия».
-- Пишутся только почта, время и название вкладки: никаких токенов и содержимого страницы.
-- Аддитивная миграция: только создаёт новую таблицу и индекс, существующие данные не трогает.
-- Повторный запуск безопасен. Пока миграция не применена, дашборд работает, просто не записывает.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS bsr_radar.session_events (
    id BIGSERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    email TEXT NOT NULL,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    section TEXT
);

CREATE INDEX IF NOT EXISTS session_events_at_idx ON bsr_radar.session_events (at DESC);

COMMIT;
