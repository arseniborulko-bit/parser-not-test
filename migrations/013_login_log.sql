-- Журнал входов в дашборд через Google: кто (рабочая почта) и когда вошёл.
--
-- Одна строка — один настоящий вход (новый вход через Google), а не каждая перерисовка страницы:
-- дашборд сам отсеивает повторы. Токены Google сюда не пишутся — только почта и время.
-- Аддитивная миграция: только создаёт новую таблицу и индекс, существующие данные не трогает.
-- Повторный запуск безопасен. Пока миграция не применена, вход работает, просто не записывается.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS bsr_radar.login_log (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL,
    logged_in_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS login_log_logged_in_at_idx ON bsr_radar.login_log (logged_in_at DESC);

COMMIT;
