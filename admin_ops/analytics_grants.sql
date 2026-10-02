-- Run as database owner in the application's database after migration 0007.
-- Existing deployment uses this read-only role; no passwords or write grants.
GRANT SELECT ON TABLE public.product_events, public.analytics_state TO newswatch_admin;
