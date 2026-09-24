-- Справочник ASIN: до сих пор ASIN существовал только внутри пары «наш товар — конкурент»,
-- поэтому завести ASIN заранее или отдельно было негде. Теперь у ASIN своя строка.
--
-- Что эта миграция НЕ делает: она не меняет то, что собирается. Сбор по-прежнему идёт по парам
-- (bsr_radar.competitor_pairs). Справочник пока описывает ASIN, а не управляет сбором — связать
-- одно с другим нужно отдельным решением, потому что каждый лишний ASIN это платный запрос.
--
-- Существующие данные не трогаются: таблица новая, заполняется из пар. Повторный запуск безопасен.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '60s';

CREATE TABLE IF NOT EXISTS bsr_radar.asins (
    id BIGSERIAL PRIMARY KEY,
    marketplace TEXT NOT NULL,
    asin TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    -- 'ours' — наш товар, 'competitor' — чужой. ASIN, который хотя бы в одной паре стоит как наш,
    -- считается нашим: то же правило, что в run_control.positions_summary.
    kind TEXT NOT NULL DEFAULT 'competitor' CHECK (kind IN ('ours', 'competitor')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (marketplace, asin)
);

-- Перенос уже заведённых ASIN из пар, чтобы справочник сразу отражал то, что есть.
INSERT INTO bsr_radar.asins (marketplace, asin, name, kind, active)
SELECT s.marketplace,
       s.asin,
       COALESCE(max(NULLIF(s.name, '')), ''),
       CASE WHEN bool_or(s.kind = 'ours') THEN 'ours' ELSE 'competitor' END,
       bool_or(s.active)
FROM (
    SELECT marketplace, our_asin AS asin, our_product AS name, 'ours' AS kind, active
    FROM bsr_radar.competitor_pairs
    UNION ALL
    SELECT marketplace, comp_asin, competitor_name, 'competitor', active
    FROM bsr_radar.competitor_pairs
) s
WHERE s.asin IS NOT NULL AND btrim(s.asin) <> ''
GROUP BY s.marketplace, s.asin
ON CONFLICT (marketplace, asin) DO NOTHING;

COMMIT;
