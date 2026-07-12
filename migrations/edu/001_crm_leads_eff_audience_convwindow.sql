-- Migration: добавить eff_leads, audience, days_to_pay_sum, days_to_pay_count в crm_leads
-- + пересоздать уникальный индекс с audience в составном ключе

ALTER TABLE crm_leads ADD COLUMN IF NOT EXISTS eff_leads integer NOT NULL DEFAULT 0;
ALTER TABLE crm_leads ADD COLUMN IF NOT EXISTS audience text NOT NULL DEFAULT 'unknown';
ALTER TABLE crm_leads ADD COLUMN IF NOT EXISTS days_to_pay_sum double precision NOT NULL DEFAULT 0;
ALTER TABLE crm_leads ADD COLUMN IF NOT EXISTS days_to_pay_count integer NOT NULL DEFAULT 0;

-- Пересоздать уникальный индекс: старый (5 полей) → новый (6 полей, +audience)
-- Старый индекс создавался через ensure_schema() в db.py
DROP INDEX IF EXISTS crm_leads_segment_key;

CREATE UNIQUE INDEX IF NOT EXISTS crm_leads_segment_key
ON crm_leads (date, campaign_id, city_ip_segment, b24_grad_year, b24_edu_level, audience);
