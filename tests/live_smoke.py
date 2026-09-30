"""Explicit operator-only live smoke; not collected by pytest.

Calls real search/model APIs, with synthetic evidence for the model comparison.
Does not create users/stories or send Telegram messages.
"""
import asyncio
from datetime import datetime, timezone

from app.config import get_settings
from app.domain import Candidate
from app.service import BotService
from app.db import close_db

async def main():
    service=BotService(get_settings())
    try:
        results=await service.search.search('OpenAI')
        assert results, 'Search returned no live materials'
        print('LIVE_SEARCH_RESULTS',len(results),'FIRST_DOMAIN',results[0].url.split('/')[2])
        page=await service.fetcher.fetch('https://www.python.org/downloads/release/python-3130/')
        assert len(page.text)>200
        print('LIVE_PUBLIC_PAGE_READ',len(page.text))
        url_story=await service.ai.extract('Следи за развитием этой версии Python.\n'+page.text[:12000],url=page.url)
        assert url_story.title and url_story.watch_goals
        print('LIVE_URL_EXTRACTION_OK',url_story.title)
        seed='Следи за публичным API проекта Atlas компании Example. Компания объявила только о разработке, дата запуска пока неизвестна.'
        extracted=await service.ai.extract(seed)
        assert extracted.search_queries and extracted.watch_goals
        print('LIVE_LLM_EXTRACTION_OK',extracted.title)
        context={'title':'Example Atlas — публичный API','current_state':'Example разрабатывает Atlas. API ещё не открыт, дата запуска неизвестна.',
                 'watch_goals':['Когда откроется публичный API?','Дата запуска'], 'known_facts':['Atlas находится в разработке.']}
        repeated=Candidate(url='https://example.org/repeat',normalized_url='https://example.org/repeat',domain='example.org',
            title='Example продолжает разработку Atlas',content_excerpt='Example повторила, что разрабатывает Atlas. Дата запуска и API по-прежнему неизвестны. Новых подробностей нет.',content_hash='a',published_at=datetime.now(timezone.utc))
        no_update=await service.ai.analyze(context,[repeated])
        assert not no_update.meaningful_update, 'Model treated a rehash as new information'
        print('LIVE_LLM_REHASH_SILENT',no_update.confidence)
        new=Candidate(url='https://example.org/official',normalized_url='https://example.org/official',domain='example.org',
            title='Официальный анонс Example: API Atlas доступен с 10 октября 2026',
            content_excerpt='В официальном заявлении компании Example объявлено: публичный API Atlas открывается 10 октября 2026 года. Ранее дата не называлась. Документация API опубликована сегодня; ключи будут выдаваться в день запуска.',
            content_hash='b',published_at=datetime.now(timezone.utc))
        update=await service.ai.analyze(context,[new])
        assert update.meaningful_update and update.source_urls==[new.url], 'Model failed new fact fixture'
        assert update.confidence>=.75 and update.novelty_score>=.65 and update.importance_score>=.55
        print('LIVE_LLM_NEW_FACT_DETECTED',update.confidence,update.novelty_score,update.importance_score)
        print('SMOKE_OK')
    finally:
        await service.close()
        await close_db()

if __name__=='__main__':
    asyncio.run(main())
