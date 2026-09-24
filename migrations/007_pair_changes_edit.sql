-- Правка уже заведённой пары (названия товара и конкурента) — новое действие 'edit' в журнале.
-- Миграция 003 разрешала только 'add', 'enable', 'disable', поэтому без этой правки запись о
-- редактировании нарушила бы ограничение и правка не сохранилась бы.
--
-- Применять нужно только там, где применена 003: сам журнал необязателен, и без него правка
-- работает, просто не попадает в историю изменений (так же ведут себя добавление и отключение).
-- Повторный запуск безопасен.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

DO $$
BEGIN
    IF to_regclass('bsr_radar.pair_changes') IS NULL THEN
        RAISE NOTICE 'Таблицы bsr_radar.pair_changes нет (миграция 003 не применялась) — пропуск.';
        RETURN;
    END IF;

    ALTER TABLE bsr_radar.pair_changes DROP CONSTRAINT IF EXISTS pair_changes_action_check;
    ALTER TABLE bsr_radar.pair_changes
        ADD CONSTRAINT pair_changes_action_check
        CHECK (action IN ('add', 'enable', 'disable', 'edit'));
END
$$;

COMMIT;
