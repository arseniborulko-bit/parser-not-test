-- Страна в ключе снимков. Раньше ключ был (дата, наш ASIN, ASIN конкурента) БЕЗ страны, поэтому
-- одна и та же пара ASIN, отслеживаемая на нескольких рынках (на 23.09.2026 таких пар 25, лишних
-- строк 28), схлопывалась в ОДНУ строку за день: данные одного рынка молча затирали другой.
--
-- Расширение ключа безопасно для существующих данных: строки, уникальные по (дата, наш, конкурент),
-- тем более уникальны по (дата, страна, наш, конкурент) — конфликт при создании нового ограничения
-- возникнуть не может. Повторный запуск безопасен.
--
-- ВАЖНО про порядок выкатки: sync_sheets_to_db.py сам определяет, какое ограничение есть в базе,
-- и подставляет подходящий ON CONFLICT. Поэтому миграцию можно применять и до, и после публикации
-- кода — синк не упадёт ни в одном из порядков.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE bsr_radar.snapshots
    DROP CONSTRAINT IF EXISTS snapshots_snapshot_date_our_asin_comp_asin_key;

ALTER TABLE bsr_radar.snapshots
    DROP CONSTRAINT IF EXISTS snapshots_day_market_pair_key;

ALTER TABLE bsr_radar.snapshots
    ADD CONSTRAINT snapshots_day_market_pair_key
    UNIQUE (snapshot_date, marketplace, our_asin, comp_asin);

COMMIT;
