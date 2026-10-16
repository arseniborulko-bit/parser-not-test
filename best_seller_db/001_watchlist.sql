-- Раздел «Best Seller подкатегории» (этап 19) — своя схема best_seller в базе дашборда (DATABASE_URL).
-- Таблицы мониторинга BSR (bsr_radar) не трогает и с ними не связана: раздел независимый. Решение владельца
-- 16.10.2026: отдельную базу не заводим.
--
-- Список ASIN, которые отслеживает раздел. Ключ — страна и ASIN: один ASIN в Германии и в Италии — две строки
-- (BSR и подкатегории там свои). Удаление — отключение (active = FALSE), чтобы можно было вернуть.

CREATE SCHEMA IF NOT EXISTS best_seller;

CREATE TABLE IF NOT EXISTS best_seller.asins (
    marketplace TEXT NOT NULL,
    asin TEXT NOT NULL CHECK (asin ~ '^B0[A-Z0-9]{8}$'),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    added_by TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (marketplace, asin)
);

-- Журнал: кто и что добавил, отключил или вернул.
CREATE TABLE IF NOT EXISTS best_seller.asin_changes (
    id BIGSERIAL PRIMARY KEY,
    at TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('add', 'enable', 'disable')),
    marketplace TEXT NOT NULL,
    asin TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS asin_changes_at_idx ON best_seller.asin_changes (at DESC);
