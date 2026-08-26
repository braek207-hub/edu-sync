-- migrations/edu/20260826_agent_experiment_registry.sql
-- Реестр гипотез поверх edu_agent_experiments (sync/agent/experiments.py,
-- docs/AGENT-EXPERIMENT-PIPELINE.md, «Этап 1 после старта беты»).
--
-- Что чинит. До этих колонок таблица была ПОСМЕРТНЫМ журналом: сторож писал
-- в неё исход уже случившегося действия (agent_e1_watchdog.action_experiment).
-- Ставка как замысел не жила нигде — ни статуса, ни горизонта, ни критерия
-- успеха, ни связи с действием, — и «сразу увидеть результат и двигаться
-- дальше» было невозможно технически: возвращаться не к чему.
--
-- status            — жизненный цикл: queued → running → won | lost |
--                     rolled_back. Значения по умолчанию нет намеренно: NULL
--                     означает «строка не реестра» (посмертная запись
--                     source='action' или квазиэксперимент source='quasi'), а
--                     DEFAULT 'queued' объявил бы 194 давно закрытых
--                     наблюдения открытыми ставками.
-- status_reason     — почему статус такой: причина читается на разборе, а не
--                     восстанавливается по датам.
-- stake_rub         — СПИСАННАЯ цена ставки. Не новый бюджет: число приходит
--                     из writer/risk.fit_into_budget, который его уже списал
--                     с недельного риск-бюджета.
-- stake_source      — каким карманом оплачена. Колонка нужна затем, чтобы
--                     появление ВТОРОГО источника денег было видно в данных.
-- horizon_days      — горизонт замера, назначенный в момент запуска, а не
--                     «посмотрим, как пойдёт».
-- success_criterion — критерий успеха словами, тоже назначенный заранее:
--                     вердикт сверяется с тем, о чём договорились, а не с
--                     тем, что удобно объявить успехом задним числом.
-- red_line          — красная линия КОПИЕЙ. Ссылка на журнал действий не
--                     годится: перепланировка ещё не применённого действия
--                     переписывает его red_line (writer/db.INSERT_ACTION_SQL),
--                     а ставка обязана помнить линию своего запуска.
-- idempotency_key   — связь с edu_agent_actions: по нему подтягивается
--                     состояние действия (применено, откатано, закрыто).
-- closed_at         — момент закрытия реестром. Не дубль measured_on: там
--                     конец окна наблюдения, здесь отметка о смене статуса.
ALTER TABLE edu_agent_experiments
  ADD COLUMN IF NOT EXISTS status            TEXT,
  ADD COLUMN IF NOT EXISTS status_reason     TEXT,
  ADD COLUMN IF NOT EXISTS stake_rub         DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS stake_source      TEXT,
  ADD COLUMN IF NOT EXISTS horizon_days      INTEGER,
  ADD COLUMN IF NOT EXISTS success_criterion TEXT,
  ADD COLUMN IF NOT EXISTS red_line          JSONB NOT NULL DEFAULT '{}'::jsonb,
  ADD COLUMN IF NOT EXISTS idempotency_key   TEXT,
  ADD COLUMN IF NOT EXISTS closed_at         TIMESTAMPTZ;

-- Открытые ставки читаются КАЖДЫМ прогоном сторожа, а закрытые копятся без
-- предела: частичный индекс держит выборку размером с очередь, а не с
-- историей. Тот же довод, что у индексов кулдауна в edu_agent_actions.
CREATE INDEX IF NOT EXISTS edu_agent_experiments_open_idx
  ON edu_agent_experiments (status)
  WHERE status IN ('queued', 'running');

-- Две ставки на одно действие — двойной счёт замера и двойное списание
-- кармана. У посмертных строк ключа нет (NULL), и частичный уникальный индекс
-- их не ограничивает.
CREATE UNIQUE INDEX IF NOT EXISTS edu_agent_experiments_idem_idx
  ON edu_agent_experiments (idempotency_key)
  WHERE idempotency_key IS NOT NULL;
