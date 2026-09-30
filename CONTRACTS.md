# Implementation contracts

Python 3.12, aiogram 3, async SQLAlchemy, PostgreSQL, Alembic. Docker Compose runs bot, worker and isolated DB. All IDs for users are Telegram numeric IDs. UTC aware times. No external analytics. Russian UI. Closed invite access; separate one-time admin claim token. No first-user-is-admin behavior.

Root owns app/config.py, app/domain.py, app/service.py, app/worker.py, app/main.py, deployment, final docs and integration tests.

Storage agent owns app/db.py, app/models.py, app/repository.py, migrations/, alembic.ini and storage tests. Pipeline agent owns app/ai/, app/search/, app/content/ and pipeline tests. UX agent owns app/bot.py and UX tests. Do not edit files owned by others. Import shared contracts; root will integrate.

## Settings (app.config.Settings)
telegram_bot_token, database_url, admin_telegram_ids (CSV string), allowed_telegram_ids (CSV string), invite_code, admin_claim_token, max_testers=10, llm_provider='disabled' ('openrouter','openai','gemini','ollama'), llm_api_key, llm_model, llm_base_url, llm_timeout_seconds=60, llm_daily_call_limit=250, llm_input_cost_per_million=0, llm_output_cost_per_million=0, search_provider='google_news' ('brave','tavily'), search_api_key, default_check_interval_hours=24, max_stories_per_user=10, max_search_queries_per_story=3, max_results_per_query=4, max_sources_per_check=6, max_manual_checks_per_day=5, manual_check_cooldown_seconds=120, demo_mode=False, log_level='INFO'. Properties admin_ids and allowed_ids -> set[int]. get_settings() cached.

## Domain (app.domain)
Pydantic StoryExtraction: title:str, short_summary:str, current_state:str, entities:list[str], keywords:list[str], search_queries:list[str], watch_goals:list[str].
Pydantic Analysis: relevant:bool, meaningful_update:bool, novelty_score:float, importance_score:float, confidence:float, new_facts:list[str], updated_state:str, reason:str, notification_summary:str, source_urls:list[str]. Scores [0,1]. Length bounds. Do not synthesize missing required fields.
Dataclass SearchResult(url,title,snippet='',published_at:datetime|None=None,query=''). Dataclass FetchedContent(url,title,text). Dataclass Candidate(url,normalized_url,domain,title,content_excerpt,content_hash,search_query='',published_at=None).
class UserError(Exception) for safe Russian user messages; ProviderUnavailable(UserError).

## Pipeline interfaces
AIClient(settings, usage_callback=None). async extract(text:str, url:str|None=None)->StoryExtraction; async analyze(story:dict, sources:list[Candidate])->Analysis; async close(). Callback async def usage_callback(operation:str, provider:str, input_tokens:int=0, output_tokens:int=0, estimated_cost:float=0, **kwargs). Provider disabled must raise ProviderUnavailable, NEVER fabricate a semantic result. Structured JSON validated, max 2 attempts; strict source URL evidence validation; no tool access. Daily budget enforced centrally by root service.
SearchClient(settings, usage_callback=None). async search(query:str)->list[SearchResult]; async close(). Google News RSS temporary free provider; Brave/Tavily adapters if possible. Bounded timeout/results, defused XML. Result links may be Google News redirects; preserve source attribution and be candid about snippets. Record each query attempted.
ContentFetcher(). async fetch(url:str)->FetchedContent; async close(). Prevent private/reserved/loopback/metadata SSRF including redirects and DNS rebinding (pinned validated public resolver); max response 1 MB, 15 seconds, max 3 redirects, allow only HTTP(S) ports 80/443, no URL credentials. Extract visible readable HTML; source content is always untrusted data.
app.search.dedupe.normalize_url(url)->str; content_hash(text)->str; is_near_duplicate(text, previous:list[str])->bool. Meaningful modifications of a URL may be skipped in this MVP and documented.

## Storage models
User: telegram_id PK, username, first_name, is_admin, created_at,last_seen_at.
Story: id integer PK,user_id FK,title,original_input,original_url,summary,current_state,entities JSON,keywords JSON,search_queries JSON,watch_goals JSON,status ('draft','active','paused','deleted'),check_frequency_hours,last_checked_at,last_meaningful_update_at,next_check_at,created_at,updated_at,lock_until nullable,lock_token nullable,last_manual_check_at.
Source: id,story_id,url,normalized_url,domain,title,published_at,fetched_at,content_hash,content_excerpt,search_query,relevance_score,is_duplicate,created_at; unique story_id+normalized_url.
StoryUpdate: id,story_id,summary,new_facts JSON,previous_state,new_state,importance_score,confidence_score,source_urls JSON,reason,is_demo,created_at,notified_at nullable,delivery_attempts default 0,delivery_locked_until nullable.
Feedback unique user_id+update_id: user_id,story_id,update_id,feedback_type ('useful','not_useful'),created_at.
UsageEvent: id,user_id nullable,story_id nullable,operation,provider,model nullable,input_tokens,output_tokens,estimated_cost,detail nullable,created_at.

app.db: engine, Session (async_sessionmaker expire_on_commit=False), init_db() (test convenience only), close_db(). Alembic production migration.
Repository(settings, session_factory=None) uses app.db.Session by default. Returned models fully loaded and detached. All user-facing operations enforce ownership. Methods async:
- get_user(user_id)->User|None; admit_user(user_id, username=None, first_name=None, is_admin=False)->User|None atomically enforce max_testers (admin may join beyond limit); admin claim global one-time enforced service via named lease or repository transaction
- create_draft(user_id, original_input, original_url, extraction:StoryExtraction)->Story (atomic max_stories_per_user including drafts, auto delete expired drafts >24h)
- activate_story(user_id,story_id)->Story; list_stories(user_id)->list[Story] exclude drafts/deleted; get_story(user_id,story_id)->Story|None
- set_status(user_id,story_id,status)->Story|None (active/paused/deleted); deletion hard-redacts input/state and removes related sources/updates/feedback while retaining anonymous usage counts
- claim_story(story_id, user_id=None, manual=False)->Story|None atomically lease 15 min random lock_token; manual rate/cooldown enforced atomically per USER daily quota, raises UserError on limit. For user_id=None automatic due only. Paused never claimed.
- due_story_ids(limit=20)->list[int]; finish_check(story_id, lock_token, error=False)->None clear own lease, update last_checked_at on success,next_check_at interval or backoff 30 minutes error
- known_sources(story_id)->list[Source] newest 100; save_check(story_id,lock_token,candidates:list[Candidate],analysis:Analysis|None)->StoryUpdate|None (atomic sources + state + update, rejects lost lease/paused/deleted, no new state when meaningful_update false). Meaningful gate root, repository should recheck analysis.meaningful_update.
- recent_updates(user_id,story_id,limit=5)->list[StoryUpdate]; feedback(user_id,update_id,kind)->bool enforce ownership/upsert.
- pending_notifications(limit=10)->list[tuple[StoryUpdate,Story]] claim delivery leases atomically for active stories, max attempts 5, no demo except created deliberately; mark_notified(update_id,success:bool)->None
- create_demo(user_id,story_id)->StoryUpdate (explicitly marked demo, not change current_state or baseline).
- record_usage(operation,provider='',user_id=None,story_id=None,input_tokens=0,output_tokens=0,estimated_cost=0,detail=None,model=None,**kwargs)->None
- usage_count_today(operation)->int; admin_stats()->dict; recent_errors(limit=10)->list[UsageEvent].

## Telegram interface
app.bot.build_router(service, settings)->aiogram.Router. app.bot.notification_text(story,update)->str and notification_keyboard(story,update)->InlineKeyboardMarkup. HTML escape all user/model text; limit text below 4096 and concise bounded lists; only safe HTTP links.
Service methods async:
- authorize(user_id,username=None,first_name=None,start_arg='')->User|None (handles allowlist, invite, admin token; persist admission)
- is_admin(user_id)->bool
- prepare_story(user_id,text)->Story (draft), confirm_story(user_id,story_id)->Story, cancel_draft(user_id,story_id)->None
- list_stories(user_id)->list[Story], get_story(user_id,story_id)->Story|None, set_status(user_id,story_id,status)->Story|None
- request_check(user_id,story_id)->str (queues due now via claim + task; returns safe message immediately; root manages runtime)
- recent_updates(user_id,story_id)->list[StoryUpdate], give_feedback(user_id,update_id,kind)->bool
- admin_summary(user_id)->str, admin_errors(user_id)->str, demo_update(user_id,story_id=None)->str
- provider_ready()->bool synchronous.

Commands /start, /help, /watching, /check_now [story_id], /admin, /admin_errors, /demo_update [story_id], /cancel. Private chats only. Middleware authorization for EVERY message and callback (except /start onboarding). Default text creates draft and displays confirmation card. Basic per-user incoming flood throttle and one in-flight extraction per user in service. Failed AI configured must clearly say admin needs API key. /start deep link admin_<token> creates first admin once; invite_<code> admits tester. Display user Telegram ID in denied message for allowlist setup. /admin show invite link constructed using bot.get_me username and configured invite_code. Do not expose admin claim token anywhere in bot UI.

Callbacks use compact action:id values; always ownership checks. Buttons watch confirm/cancel, story check/pause/resume/delete (confirmation step), history, future chat. Feedback useful/not_useful.
