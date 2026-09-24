-- Рейтинг и число отзывов в снимках. Провайдер отдаёт их в том же ответе, что BSR и цену
-- (scraping.parse_product уже читает average_rating и total_reviews), но до базы они не доходили:
-- в листах и в snapshots для них просто не было места. Дополнительных платных запросов не нужно.
--
-- Все четыре столбца допускают NULL и остаются пустыми у всей старой истории: заполнять её
-- сегодняшними значениями задним числом нельзя — это исказило бы историю.
-- NULL означает «данных нет», а 0 отзывов — настоящий ноль; это разные вещи.
--
-- Аддитивная миграция, существующие строки не трогает. Повторный запуск безопасен.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

ALTER TABLE bsr_radar.snapshots
    ADD COLUMN IF NOT EXISTS our_rating NUMERIC(2, 1),
    ADD COLUMN IF NOT EXISTS our_reviews_count INTEGER,
    ADD COLUMN IF NOT EXISTS comp_rating NUMERIC(2, 1),
    ADD COLUMN IF NOT EXISTS comp_reviews_count INTEGER;

-- Рейтинг Amazon — от 0 до 5, отзывов не бывает отрицательное число.
-- NOT VALID: проверка применяется только к новым строкам, старые не перечитываются.
ALTER TABLE bsr_radar.snapshots DROP CONSTRAINT IF EXISTS snapshots_rating_range_check;
ALTER TABLE bsr_radar.snapshots
    ADD CONSTRAINT snapshots_rating_range_check
    CHECK (
        (our_rating IS NULL OR (our_rating >= 0 AND our_rating <= 5))
        AND (comp_rating IS NULL OR (comp_rating >= 0 AND comp_rating <= 5))
        AND (our_reviews_count IS NULL OR our_reviews_count >= 0)
        AND (comp_reviews_count IS NULL OR comp_reviews_count >= 0)
    ) NOT VALID;

COMMIT;
