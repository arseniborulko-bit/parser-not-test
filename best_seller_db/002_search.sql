-- Раздел «Best Seller подкатегории» (этап 19): кэш подкатегорий и результаты поиска. Та же схема best_seller в базе
-- дашборда, что и 001_watchlist.sql; таблицы мониторинга (bsr_radar) не трогает. Повторный запуск безопасен.

-- Кэш подкатегории (узла Best Sellers) на неделю: один узел проверяется один раз для всех ASIN.
-- Ключ — страна и номер узла. Поля заполняются по мере проверки: если К1 не пройден, топ 51–100 не запрашивается.
CREATE TABLE IF NOT EXISTS best_seller.node_cache (
    marketplace TEXT NOT NULL,
    node TEXT NOT NULL CHECK (node ~ '^[0-9]+$'),
    name TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    leader_asin TEXT,
    leader_bsr INTEGER CHECK (leader_bsr IS NULL OR leader_bsr > 0),
    leader_badge BOOLEAN,            -- живой бейдж «#1 Best Seller» у лидера (К4); NULL — не проверяли
    item_count INTEGER CHECK (item_count IS NULL OR item_count BETWEEN 0 AND 100),  -- К2; NULL — не проверяли
    top JSONB NOT NULL DEFAULT '[]', -- [{"rank": 1, "asin": "...", "title": "..."}] — заголовки для К3
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (marketplace, node)
);

-- Один поиск по одному ASIN: кто запустил, наш BSR и цель, статус.
CREATE TABLE IF NOT EXISTS best_seller.searches (
    id BIGSERIAL PRIMARY KEY,
    marketplace TEXT NOT NULL,
    asin TEXT NOT NULL CHECK (asin ~ '^B0[A-Z0-9]{8}$'),
    actor TEXT NOT NULL DEFAULT '',
    our_bsr INTEGER,
    target_bsr INTEGER CHECK (target_bsr IS NULL OR target_bsr > 0),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'done', 'not_needed', 'error')),
    error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS searches_asin_idx ON best_seller.searches (marketplace, asin, started_at DESC);

-- Решение по каждой подкатегории в поиске: критерии К1–К4 (NULL — не проверяли), релевантность, приоритет.
CREATE TABLE IF NOT EXISTS best_seller.search_results (
    search_id BIGINT NOT NULL REFERENCES best_seller.searches (id) ON DELETE CASCADE,
    node TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    leader_bsr INTEGER,
    k1 BOOLEAN, k2 BOOLEAN, k3 BOOLEAN, k4 BOOLEAN,
    relevance TEXT CHECK (relevance IS NULL OR relevance IN ('relevant', 'semi', 'risky', 'irrelevant')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'suitable', 'rejected')),
    priority TEXT,
    reason TEXT NOT NULL DEFAULT '',
    written_to_sheet BOOLEAN NOT NULL DEFAULT FALSE,  -- записано в таблицу гипотез (этап 3)
    PRIMARY KEY (search_id, node)
);
