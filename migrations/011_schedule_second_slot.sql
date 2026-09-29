-- Второй слот времени автосбора.
--
-- bsr_radar.schedule раньше допускал только id=1 (CHECK schedule_single_row) — одно время в сутки.
-- Владелец попросил второй независимый слот (id=2), чтобы сбор реально мог запускаться дважды в
-- день. Строки по-прежнему необязательны: нет строки id=N — слот N выключен, как и раньше для id=1.
--
-- Идемпотентно: снятие несуществующего constraint и добавление уже существующего игнорируются.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

ALTER TABLE bsr_radar.schedule DROP CONSTRAINT IF EXISTS schedule_single_row;

DO $$
BEGIN
    ALTER TABLE bsr_radar.schedule ADD CONSTRAINT schedule_two_slots CHECK (id IN (1, 2));
EXCEPTION
    WHEN duplicate_object THEN
        NULL;
END
$$;

COMMIT;
