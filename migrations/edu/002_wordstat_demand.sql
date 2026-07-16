-- EDU «Спрос рынка»: недельный рыночный спрос из Wordstat, ПО-ФРАЗНО (не Σ).
-- Σ по неделе считается на чтении (роут) → гибкость состава фраз без ре-бэкфилла.
-- Крупные непересекающиеся корни, регион ru (225), широкое соответствие.
CREATE TABLE IF NOT EXISTS edu_wordstat_demand (
  week_start date    NOT NULL,          -- ISO-понедельник недели
  region     text    NOT NULL DEFAULT 'ru',
  phrase     text    NOT NULL,          -- отдельная фраза (не сумма)
  frequency  integer NOT NULL,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (week_start, region, phrase)
);

ALTER TABLE edu_wordstat_demand ENABLE ROW LEVEL SECURITY;

-- Чтение через сервисную роль/анон по образцу прочих таблиц (RLS вкл., read-only политика).
DROP POLICY IF EXISTS edu_wordstat_demand_read ON edu_wordstat_demand;
CREATE POLICY edu_wordstat_demand_read ON edu_wordstat_demand FOR SELECT USING (true);