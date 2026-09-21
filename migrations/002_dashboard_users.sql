-- Этап 1: вход и роли дашборда. Аддитивная миграция: только создаёт новую таблицу,
-- существующие таблицы и данные не трогает. Повторный запуск безопасен.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '10s';

CREATE TABLE IF NOT EXISTS bsr_radar.dashboard_users (
    email TEXT PRIMARY KEY CHECK (email = lower(email)),
    role TEXT NOT NULL CHECK (role IN ('admin', 'editor')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    added_by TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMIT;
