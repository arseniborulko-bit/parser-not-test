-- Управление парами из дашборда: журнал изменений и индекс по снимкам пары.
-- Аддитивная миграция: создаёт новую таблицу и индексы, существующие данные не трогает. Повторный запуск безопасен.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS parser_not_test.pair_changes (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'enable', 'disable')),
    marketplace TEXT NOT NULL,
    our_asin TEXT NOT NULL,
    comp_asin TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS pair_changes_at_idx ON parser_not_test.pair_changes (at DESC);

-- Нужен «Текущему состоянию» и названиям в списке пар: последний снимок каждой пары.
CREATE INDEX IF NOT EXISTS snapshots_pair_date_idx
    ON parser_not_test.snapshots (our_asin, comp_asin, snapshot_date DESC);

COMMIT;
