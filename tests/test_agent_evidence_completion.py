"""Adversarial checks for evidence completion deadlines, reuse and proposal safety."""
import asyncio
import json
from types import MethodType

import pytest

from archbro.backend.core.contracts import AgentActionType, Component
from archbro.backend.core.evaluation import DriftClassification
from test_agent_mcp_tools import _session, _gemini_provider, _project_context, _verification_event, _aligned_wire
from test_agent_evidence_acceptance import drift_wire


def read(session):
    return session.call('get_file_contents', {'owner': 'Magic-Dala', 'repo': 'archbro', 'path': 'README.md', 'ref': 'dev2'})


def generate(provider, session):
    context = _project_context()
    context.architecture.components = [Component(id='database', name='SQLite', type='database', responsibility='Persist state.')]
    return asyncio.run(provider.generate_with_external_tools(event=_verification_event(context.project.id), context=context, system_prompt='Respect evidence and human approval.', external_tools=session))


def test_equal_model_and_total_deadlines_still_reserve_a_synthesis_turn():
    provider = _gemini_provider()
    provider.tool_interaction_model_timeout_seconds = 0.12
    provider.tool_interaction_total_timeout_seconds = 0.12
    session, gateway = _session()
    invocations = []

    async def invoke(self, model, prompt, *, tools=None, evidence_completion=False):
        invocations.append((bool(tools), evidence_completion))
        if tools:
            read(session)
            await asyncio.sleep(1)
        assert evidence_completion
        return _aligned_wire('Completed inside the original overall deadline.')

    provider._invoke = MethodType(invoke, provider)
    assert generate(provider, session).summary == 'Completed inside the original overall deadline.'
    assert invocations == [(True, False), (False, True)]
    assert len(gateway.calls) == 1


@pytest.mark.parametrize('failure', ['provider_error', 'timeout'])
def test_fallback_synthesizes_without_repeating_github_reads(failure):
    provider = _gemini_provider()
    provider.fallback_model_ids = ('gemini-fallback',)
    provider.tool_interaction_model_timeout_seconds = 0.06
    provider.tool_interaction_total_timeout_seconds = 0.6
    session, gateway = _session()
    calls = []

    async def invoke(self, model, prompt, *, tools=None, evidence_completion=False):
        calls.append((model, bool(tools), evidence_completion))
        if tools:
            read(session)
            if failure == 'provider_error':
                raise RuntimeError('503 UNAVAILABLE')
            await asyncio.sleep(1)
        if model == provider.model_id:
            await asyncio.sleep(1)
        assert evidence_completion
        return _aligned_wire('Fallback reused the private evidence.')

    provider._invoke = MethodType(invoke, provider)
    assert generate(provider, session).summary == 'Fallback reused the private evidence.'
    assert len(gateway.calls) == 1
    assert calls[-1] == ('gemini-fallback', False, True)
    assert sum(bool(tools) for _, tools, _ in calls) == 1
    if failure == 'provider_error':
        assert len(calls) == 2


@pytest.mark.parametrize('when', ['before_synthesis', 'during_synthesis'])
def test_scope_change_discards_evidence_without_exposing_checker_details(when, caplog):
    provider = _gemini_provider()
    provider.tool_interaction_model_timeout_seconds = 0.06
    provider.tool_interaction_total_timeout_seconds = 0.6
    session, gateway = _session()
    scope_valid = True
    synthesis_calls = []

    def check():
        if not scope_valid:
            raise ValueError('private-account-secret-must-not-leak')

    session.scope_check = check

    async def invoke(self, model, prompt, *, tools=None, evidence_completion=False):
        nonlocal scope_valid
        if tools:
            read(session)
            if when == 'before_synthesis':
                scope_valid = False
            await asyncio.sleep(1)
        synthesis_calls.append(model)
        scope_valid = False
        return drift_wire()

    provider._invoke = MethodType(invoke, provider)
    with pytest.raises(PermissionError, match='scope changed') as error:
        generate(provider, session)
    assert 'private-account-secret' not in str(error.value) + caplog.text
    assert session.has_successful_evidence is False
    assert session.evidence_references() == []
    assert len(gateway.calls) == 1
    assert len(synthesis_calls) == (0 if when == 'before_synthesis' else 1)


def test_scope_is_rechecked_before_a_fallback_model_receives_evidence():
    provider = _gemini_provider()
    provider.fallback_model_ids = ('gemini-fallback',)
    provider.tool_interaction_model_timeout_seconds = 0.06
    session, _ = _session()
    scope_valid = True
    completions = []

    def check():
        if not scope_valid:
            raise PermissionError('account changed')

    session.scope_check = check

    async def invoke(self, model, prompt, *, tools=None, evidence_completion=False):
        nonlocal scope_valid
        if tools:
            read(session)
            await asyncio.sleep(1)
        completions.append(model)
        scope_valid = False
        raise RuntimeError('503 UNAVAILABLE')

    provider._invoke = MethodType(invoke, provider)
    with pytest.raises(PermissionError):
        generate(provider, session)
    assert completions == [provider.model_id]


@pytest.mark.parametrize('invalid', ['missing_proposal', 'unsupported_change', 'foreign_component'])
def test_invalid_synthesis_proposal_returns_unverified_without_mutation(invalid):
    provider = _gemini_provider()
    provider.tool_interaction_model_timeout_seconds = 0.04
    provider.tool_interaction_total_timeout_seconds = 0.4
    session, _ = _session()

    async def invoke(self, model, prompt, *, tools=None, evidence_completion=False):
        if tools:
            read(session)
            await asyncio.sleep(1)
        wire = drift_wire()
        if invalid == 'missing_proposal':
            wire.architecture_proposal = None
            wire.architecture_review_required = False
        elif invalid == 'unsupported_change':
            wire.architecture_proposal.proposed_changes[0]['operation'] = 'add_component'
        else:
            wire.architecture_proposal.proposed_changes[0]['component_id'] = 'other-project-component'
        return wire

    provider._invoke = MethodType(invoke, provider)
    decision = generate(provider, session)
    assert decision.evaluation.classification == DriftClassification.INSUFFICIENT_EVIDENCE
    assert decision.architecture_review_required is False
    assert [a.type for a in decision.actions] == [AgentActionType.NO_ACTION]
    assert 'UNVERIFIED' in decision.summary


def test_completion_payload_bound_includes_json_escaping_and_scope():
    session, _ = _session()
    read(session)
    cached = next(iter(session._cache.values()))['response']
    cached['external_evidence'] = {'text': '\\"\n' * 15000}
    result = session.completion_evidence_context(max_chars=2500)
    encoded = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    assert len(encoded) <= 2500
    assert result['sources'][0]['truncated_for_completion'] is True
    assert result['repository_scope']['full_name'] == 'Magic-Dala/archbro'
    assert 'UNVERIFIED' in result['coverage_rule']


def test_protected_provider_error_is_not_hidden_by_evidence_fallback():
    provider = _gemini_provider()
    session, _ = _session()
    calls = []

    async def invoke(self, model, prompt, *, tools=None, evidence_completion=False):
        calls.append(model)
        read(session)
        raise RuntimeError('403 PERMISSION_DENIED')

    provider._invoke = MethodType(invoke, provider)
    with pytest.raises(RuntimeError, match='PERMISSION_DENIED'):
        generate(provider, session)
    assert len(calls) == 1
