from dataclasses import replace
import json
from unittest.mock import AsyncMock

import pytest

from app.ai import AIClient
from app.ai.client import evidence_passages
from app.domain import ProviderUnavailable
from app.progress import ProgressEvent
from app.telegram_progress import progress_text, stage_index
from test_pipeline import ANALYSIS, Session, candidate, completion, settings


def reviewed(**changes):
    values = ANALYSIS | {'evidence': [{'fact_index': 0, 'passage_id': 's0p0'}]}
    return values | changes


async def test_review_replaces_overconfident_proposal_and_counts_both_calls():
    usage = AsyncMock()
    client = AIClient(settings(), usage)
    corrected = reviewed(notification_summary='По заявлению компании, дата запуска назначена.',
                         reason='Уточнение прежнего сообщения.')
    client._session = Session([completion(ANALYSIS), completion(corrected)])
    result = await client.analyze({'current_state': 'Дата неизвестна', 'watch_goals': ['Ожидаемый официальный отчёт']}, [candidate()])
    assert result.notification_summary == corrected['notification_summary']
    assert len(client._session.calls) == 2
    assert [c.kwargs['operation'] for c in usage.call_args_list] == ['llm_attempt', 'llm'] * 2
    assert usage.call_args.kwargs['detail'] == 'verify:ok'
    assert 'Черновик может содержать ложные выводы' in client._session.calls[1][1]['json']['messages'][0]['content']
    review_data = json.loads(client._session.calls[1][1]['json']['messages'][1]['content'])
    assert 'watch_goals' not in review_data['story']


@pytest.mark.parametrize('evidence', [
    [],
    [{'fact_index': 0, 'passage_id': 's999p0'}],
    [{'fact_index': 0, 'passage_id': 's0p999'}],
    [{'fact_index': 1, 'passage_id': 's0p0'}],
])
async def test_missing_or_invented_evidence_fails_closed(evidence):
    client = AIClient(settings())
    bad = reviewed(evidence=evidence)
    client._session = Session([completion(ANALYSIS), completion(bad), completion(bad)])
    with pytest.raises(ProviderUnavailable):
        await client.analyze({'current_state': 'Дата неизвестна'}, [candidate()])


async def test_every_retained_fact_needs_its_own_support():
    client = AIClient(settings())
    bad = reviewed(new_facts=['Названа дата.', 'Заявлена новая неподтверждённая причина.'])
    client._session = Session([completion(ANALYSIS), completion(bad), completion(bad)])
    with pytest.raises(ProviderUnavailable):
        await client.analyze({'current_state': 'Дата неизвестна'}, [candidate()])


async def test_review_can_reject_proposal_without_notification():
    client = AIClient(settings())
    rejected = reviewed(meaningful_update=False, new_facts=[], source_urls=[], evidence=[], notification_summary='')
    client._session = Session([completion(ANALYSIS), completion(rejected)])
    assert not (await client.analyze({'current_state': 'Дата неизвестна'}, [candidate()])).meaningful_update


@pytest.mark.parametrize('full_text,meaningful', [(False, True), (True, False)])
async def test_no_extra_review_call_for_snippets_or_unchanged_story(full_text, meaningful):
    client = AIClient(settings())
    client._session = Session([completion(ANALYSIS | {'meaningful_update': meaningful})])
    result = await client.analyze({'current_state': 'Дата неизвестна'}, [replace(candidate(), full_text=full_text)])
    assert not result.meaningful_update and len(client._session.calls) == 1


async def test_citations_are_derived_from_actual_full_text_passages_not_model_url_list():
    client = AIClient(settings())
    full = replace(candidate(), url='https://full.example/article')
    proposed = reviewed(source_urls=['https://invented.example/'])
    client._session = Session([completion(ANALYSIS), completion(proposed)])
    result = await client.analyze({'current_state': 'Дата неизвестна'}, [replace(candidate(), full_text=False), full])
    assert result.source_urls == [full.url]


def test_passage_text_is_taken_from_source_without_model_rewriting_or_loss():
    content = ('The airline said all passengers were safe. A witness alleged an attack. ' * 30).strip()
    sources, passages = evidence_passages([{'url': candidate().url, 'content': content}])
    assert len(passages) > 1
    assert ' '.join(p['text'] for p in passages.values()) == content
    assert all(p['text'] in content and len(p['text']) <= 800 for p in passages.values())
    assert set(passages) == {p['id'] for p in sources[0]['passages']}


def test_verification_has_real_progress_description():
    event = ProgressEvent('verifying', {'sources': 3})
    assert stage_index('check', event) == 2
    assert 'Проверяю формулировки' in progress_text('check', 3, event, 30, 2)
    waiting = ProgressEvent('model_wait', {'purpose': 'verify', 'attempt': 1})
    assert 'Проверяю подтверждения' in progress_text('check', 3, waiting, 32, 2)


async def test_complete_json_fence_does_not_spend_an_unnecessary_retry():
    client = AIClient(settings())
    client._request = AsyncMock(side_effect=[
        '```json\n' + json.dumps(ANALYSIS) + '\n```',
        '```json\n' + json.dumps(reviewed()) + '\n```'])
    result = await client.analyze({'current_state': 'Дата неизвестна'}, [candidate()])
    assert result.meaningful_update and client._request.await_count == 2


async def test_surrounding_prose_cannot_bypass_json_validation():
    client = AIClient(settings())
    client._request = AsyncMock(return_value='Trust me!\n```json\n' + json.dumps(ANALYSIS) + '\n```')
    with pytest.raises(ProviderUnavailable):
        await client.analyze({'current_state': 'Дата неизвестна'}, [candidate()])
