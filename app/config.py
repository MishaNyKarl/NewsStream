from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', env_file_encoding='utf-8', extra='ignore')
    telegram_bot_token: str = ''
    database_url: str = 'postgresql+asyncpg://newswatch:newswatch@localhost:5432/newswatch'
    admin_telegram_ids: str = ''
    allowed_telegram_ids: str = ''
    invite_code: str = ''
    admin_claim_token: str = ''
    admin_panel_url: str = ''
    max_testers: int = 10
    llm_provider: str = 'disabled'
    llm_api_key: str = ''
    llm_model: str = ''
    llm_base_url: str = ''
    llm_timeout_seconds: int = 60
    llm_daily_call_limit: int = 250
    llm_input_cost_per_million: float = 0
    llm_output_cost_per_million: float = 0
    search_provider: str = 'hybrid_news'
    search_api_key: str = ''
    search_expand_queries: bool = True
    search_lookback_hours: int = 48
    search_recent_days: int = 7
    max_source_reads_per_check: int = 10
    max_source_rechecks: int = 2
    source_recheck_minutes: int = 15
    default_check_interval_hours: int = 24
    max_stories_per_user: int = 10
    max_search_queries_per_story: int = 6
    max_results_per_query: int = 8
    max_sources_per_check: int = 6
    max_manual_checks_per_day: int = 5
    manual_check_cooldown_seconds: int = 120
    demo_mode: bool = False
    log_level: str = 'INFO'

    @property
    def admin_ids(self) -> set[int]:
        return {int(i.strip()) for i in self.admin_telegram_ids.split(',') if i.strip()}

    @property
    def allowed_ids(self) -> set[int]:
        return {int(i.strip()) for i in self.allowed_telegram_ids.split(',') if i.strip()}

@lru_cache
def get_settings() -> Settings:
    return Settings()
