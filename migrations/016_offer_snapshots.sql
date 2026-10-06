-- Продавцы на карточках наших товаров (ScrapingDog Amazon Offers API): кто продаёт и у кого Buy Box.
--
-- Одна строка — одно предложение (продавец) на карточке ASIN в момент проверки. ASIN, на котором
-- нет ни одного предложения, — одна строка с пустым продавцом («проверено, пусто»). Все строки одной
-- проверки пишутся одной транзакцией и получают одинаковый checked_at (now() транзакции).
-- ours: TRUE — наш продавец, FALSE — чужой, NULL — «наш» на момент проверки не был задан.
-- Аддитивная миграция: только создаёт новую таблицу и индекс, существующие данные не трогает.
-- Повторный запуск безопасен. Пока миграция не применена, проверка продавцов идёт, но не записывается.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS bsr_radar.offer_snapshots (
    id BIGSERIAL PRIMARY KEY,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    marketplace TEXT NOT NULL,
    asin TEXT NOT NULL,
    product TEXT,
    seller_id TEXT,
    seller_name TEXT,
    price NUMERIC(12, 2),
    currency TEXT,
    is_new BOOLEAN,
    fba BOOLEAN,
    buybox BOOLEAN NOT NULL DEFAULT FALSE,
    ours BOOLEAN
);

CREATE INDEX IF NOT EXISTS offer_snapshots_asin_checked_idx
    ON bsr_radar.offer_snapshots (marketplace, asin, checked_at DESC);

COMMIT;
