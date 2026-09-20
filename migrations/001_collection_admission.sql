-- Этап 2А. Подготовленная миграция; автоматически НЕ выполняется.
-- Перед применением: отдельное согласование и проверка на тестовой PostgreSQL.
-- Старые записи/статусы не изменяет; старые running продолжат блокировать сбор.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '10s';

ALTER TABLE parser_not_test.collection_runs
    ADD COLUMN IF NOT EXISTS owner_key TEXT,
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

COMMIT;
