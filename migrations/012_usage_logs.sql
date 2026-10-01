-- Журнал использования инструментов (usage tracking): кто, когда, в каком инструменте, какой объём работы.
--
-- Таблица общая для всех инструментов команды (Competitor BSR пишет tool_name = 'Competitor BSR'),
-- поэтому она в схеме public, а не в bsr_radar: сводный дашборд читает одну таблицу.
-- Аддитивная миграция: только создаёт новую таблицу и индексы, существующие данные не трогает.
-- Повторный запуск безопасен. Пока миграция не применена, дашборд работает как раньше, без записи.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

CREATE TABLE IF NOT EXISTS public.usage_logs (
    id BIGSERIAL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_name TEXT NOT NULL,           -- email (вход через Google), имя (пароль команды) или «Команда»
    tool_name TEXT NOT NULL,           -- 'Competitor BSR' и т.п.
    action TEXT NOT NULL,              -- 'collect_all', 'spot_check', 'pairs_add' и т.п.
    volume INTEGER CHECK (volume IS NULL OR volume >= 0),   -- объём работы: сколько ASIN, пар, строк
    unit TEXT                          -- в чём измерен volume: 'ASIN', 'пар', 'строк'
);

CREATE INDEX IF NOT EXISTS usage_logs_tool_created_idx ON public.usage_logs (tool_name, created_at DESC);
CREATE INDEX IF NOT EXISTS usage_logs_created_idx ON public.usage_logs (created_at DESC);

COMMIT;
