-- Частичный сбор (кнопки "Собрать наши"/"Собрать конкурентов"): у каждой области сбора свой
-- дневной лимит попыток и правило "уже был успешный сбор сегодня", отдельно от обычного полного сбора.
-- Аддитивная миграция: новый столбец со значением по умолчанию 'all' у всех существующих строк
-- (они и были полным сбором), существующие данные не трогает. Повторный запуск безопасен.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE bsr_radar.collection_runs
    ADD COLUMN IF NOT EXISTS scope TEXT NOT NULL DEFAULT 'all';

ALTER TABLE bsr_radar.collection_runs
    DROP CONSTRAINT IF EXISTS collection_runs_scope_check;

ALTER TABLE bsr_radar.collection_runs
    ADD CONSTRAINT collection_runs_scope_check CHECK (scope IN ('all', 'ours', 'competitors'));

COMMIT;
