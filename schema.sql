-- Схема под проект "parser not test" (Amazon BSR competitor tracker).
-- Живёт в общей базе рядом с другими проектами — ничего вне этой схемы не трогаем.

CREATE SCHEMA IF NOT EXISTS parser_not_test;

CREATE TABLE IF NOT EXISTS parser_not_test.competitor_pairs (
    id SERIAL PRIMARY KEY,
    marketplace TEXT NOT NULL,
    our_asin TEXT NOT NULL,
    our_product TEXT,
    comp_asin TEXT NOT NULL,
    competitor_name TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (marketplace, our_asin, comp_asin)
);

CREATE TABLE IF NOT EXISTS parser_not_test.snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_date DATE NOT NULL,
    marketplace TEXT NOT NULL,
    currency TEXT,
    our_asin TEXT NOT NULL,
    our_product TEXT,
    our_price NUMERIC,
    our_bsr INTEGER,
    our_bsr_delta_24h NUMERIC,
    comp_asin TEXT NOT NULL,
    competitor_name TEXT,
    comp_price NUMERIC,
    comp_bsr INTEGER,
    comp_bsr_delta_24h NUMERIC,
    comp_stock TEXT,
    price_diff_pct NUMERIC,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (snapshot_date, our_asin, comp_asin)
);

-- Одна строка (id = 1) — время ежедневного автозапуска парсера. Раньше это
-- время задавалось в листе Config Google Таблицы; теперь дашборд пишет его
-- сюда и сразу же перепрограммирует задачу Планировщика Windows.
CREATE TABLE IF NOT EXISTS parser_not_test.schedule (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    hour SMALLINT NOT NULL CHECK (hour BETWEEN 0 AND 23),
    minute SMALLINT NOT NULL CHECK (minute BETWEEN 0 AND 59),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT schedule_single_row CHECK (id = 1)
);
