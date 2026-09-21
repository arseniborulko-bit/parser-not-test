-- Схема под проект "parser not test" (Amazon BSR competitor tracker).
-- Живёт в общей базе рядом с другими проектами — ничего вне этой схемы не трогаем.

CREATE SCHEMA IF NOT EXISTS bsr_radar;

CREATE TABLE IF NOT EXISTS bsr_radar.competitor_pairs (
    id SERIAL PRIMARY KEY,
    marketplace TEXT NOT NULL,
    our_asin TEXT NOT NULL,
    our_product TEXT,
    comp_asin TEXT NOT NULL,
    competitor_name TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    UNIQUE (marketplace, our_asin, comp_asin)
);

CREATE TABLE IF NOT EXISTS bsr_radar.snapshots (
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
CREATE TABLE IF NOT EXISTS bsr_radar.schedule (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    hour SMALLINT NOT NULL CHECK (hour BETWEEN 0 AND 23),
    minute SMALLINT NOT NULL CHECK (minute BETWEEN 0 AND 59),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT schedule_single_row CHECK (id = 1)
);

-- История запусков (ручных и автоматических, локальных и из GitHub Actions).
-- Нужна дашборду и любому облачному раннеру, чтобы видеть статус — локальный
-- run_status.json из облака не виден.
CREATE TABLE IF NOT EXISTS bsr_radar.collection_runs (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,              -- 'local' | 'github_actions' и т.п.
    step TEXT NOT NULL,                -- 'parser' | 'sync'
    status TEXT NOT NULL,              -- 'running' | 'done' | 'error'
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    error TEXT,
    owner_key TEXT,                    -- владелец разрешения (workflow + attempt / local UUID)
    claimed_at TIMESTAMPTZ             -- однократное использование разрешения
);

-- Для существующей таблицы нужен отдельный migrations/001_collection_admission.sql.
-- CREATE TABLE IF NOT EXISTS выше не добавляет колонки в существующие таблицы.

CREATE INDEX IF NOT EXISTS collection_runs_started_at_idx
    ON bsr_radar.collection_runs (started_at DESC);

-- Зеркало листа "Подписчики" — кто написал Telegram-боту, чтобы получать отчёты.
CREATE TABLE IF NOT EXISTS bsr_radar.telegram_subscribers (
    telegram_id TEXT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    subscribed_at TIMESTAMPTZ,
    active BOOLEAN NOT NULL DEFAULT TRUE
);
