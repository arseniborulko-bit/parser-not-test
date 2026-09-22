-- Фото товара в "Текущем состоянии": ссылка на картинку от ScrapingDog (main_image), отдельно для нашего товара и конкурента.
-- Аддитивная миграция: добавляет два необязательных (NULL допустим) столбца, существующие данные не трогает. Повторный запуск безопасен.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE bsr_radar.snapshots
    ADD COLUMN IF NOT EXISTS our_image_url TEXT,
    ADD COLUMN IF NOT EXISTS comp_image_url TEXT;

COMMIT;
