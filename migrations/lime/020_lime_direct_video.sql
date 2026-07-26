-- Видео-досмотры медийных кампаний Директа (REACH_AND_FREQUENCY_PERFORMANCE_REPORT).
-- video_complete = досмотры 100% (как watchedVideo100 у Urban); video_views = просмотры.
-- Идемпотентно: apply_lime_migrations гоняет все .sql каждый запуск.
ALTER TABLE lime_direct_stats ADD COLUMN IF NOT EXISTS video_complete integer NOT NULL DEFAULT 0;
ALTER TABLE lime_direct_stats ADD COLUMN IF NOT EXISTS video_views integer NOT NULL DEFAULT 0;
