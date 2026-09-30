"""Narrow candidate-to-canonical reply pipeline."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
import hashlib
import json
import os
import re
from datetime import datetime
from typing import Any, Mapping, Protocol

from persona_assembly import UntrustedFragment, assemble_persona
from persona_loader import load_persona
from runtime.persona.persona_mode import persona_mode_for_reply_mode
from reply_model_quality import create_model_quality_ports, resolve_model_quality_config
from runtime.reply.reply_context import ReplyContext, ReplyMode
from runtime.reply.current_turn_interpretation import (
    CurrentTurnInterpreter, projection_messages,
)
from reply_orchestrator import ReplyRequest, ReplyResult, ReplyState
from runtime.reply.reply_quality_gate import (
    DeliveryRepairDisposition,
    ReviewerPort,
    RewriterPort,
    run_reply_quality_gate,
)
from runtime.reply.reply_reviewer import (
    NullReviewer,
    TrustedCharacterReply,
    TrustedReviewEvidence,
)
from runtime.memory.memory_port import CONVERSATION_MEMORY, MemoryRecord
from runtime.letter_stickers.selection import (
    allowed_stickers, selection_instruction, split_selection, weighted_candidates,
)
from runtime.reply.letter_presentation import LETTER_PRESENTATION_INSTRUCTION, split_signature
from runtime.reply.prompt_budget import PromptBudgetExceeded


_CHARACTER_REPLY_HISTORY_LIMIT = 1200
_CHARACTER_REPLY_PREFIX = "character_reply: "
_PERSONA_NOT_READY = "PERSONA_NOT_READY"


class _PersonaNotReadyError(RuntimeError):
    """Configured Letter generation cannot publish a non-ready Persona package."""


class _RecallBudgetExceeded(RuntimeError):
    code = 'RECALL_CONTEXT_BUDGET_EXCEEDED'


class _WorldSelectionBudgetExceeded(_RecallBudgetExceeded):
    code = 'JEV_WORLD_SELECTION_BUDGET'


class OrchestratorPort(Protocol):
    async def run(self, request: object) -> ReplyResult: ...


class CurrentTurnInterpreterPort(Protocol):
    async def interpret(self, user_text: str) -> dict[str, Any]: ...


def current_turn_interpretation_enabled() -> bool:
    return os.environ.get("OLIVIA_LETTER_CURRENT_TURN_INTERPRETATION", "").strip().casefold() in {
        "1", "true", "yes", "on",
    }


def _runtime_current_turn_interpreter(orchestrator: object) -> CurrentTurnInterpreter | None:
    if not current_turn_interpretation_enabled():
        return None
    bridge = getattr(orchestrator, "gateway", None)
    adapter = getattr(bridge, "adapter", None)
    gateway = getattr(adapter, "gateway", None)
    if gateway is None:
        raise RuntimeError("CURRENT_TURN_INTERPRETATION_UNAVAILABLE")
    config = resolve_model_quality_config(getattr(adapter, "config", None))
    return CurrentTurnInterpreter(
        gateway, timeout_seconds=config.reasoning_timeout_seconds or config.timeout_seconds,
    )


class UnavailableRewriter:
    def rewrite(
        self,
        candidate: str,
        context: ReplyContext,
        violation_codes: tuple[str, ...],
    ) -> str:
        raise RuntimeError("rewriter is unavailable")


@dataclass(frozen=True)
class PipelineResult:
    request_id: str
    state: ReplyState
    text: str = ""
    error_code: str | None = None
    retryable: bool = False
    quality_status: str | None = None
    violation_codes: tuple[str, ...] = ()
    reviewer_calls: int = 0
    rewrite_calls: int = 0
    sticker_id: str | None = None
    signature: str | None = None
    semantic_shadow_task: asyncio.Task | None = field(default=None, repr=False, compare=False)
    delivery_repair_disposition: DeliveryRepairDisposition = (
        DeliveryRepairDisposition.NONE
    )
    expression_context: dict | None = None
    companion_decision: dict | None = field(default=None, repr=False)
    companion_timing: str | None = None
    companion_delivery: str | None = None
    proactive_decision: dict | None = field(default=None, repr=False)


@dataclass(frozen=True)
class _PreparedGeneration:
    request: object
    trusted_evidence: TrustedReviewEvidence = TrustedReviewEvidence()
    adopted_world: dict | None = None
    persona_snapshot: object | None = None


@dataclass(frozen=True)
class _SelectedHistory:
    fragments: tuple[UntrustedFragment, ...]
    trusted_evidence: TrustedReviewEvidence


class ReplyPipeline:
    def __init__(
        self,
        orchestrator: OrchestratorPort,
        *,
        reviewer: ReviewerPort,
        rewriter: RewriterPort,
        discover_runtime_ports: bool = True,
        current_turn_interpreter: CurrentTurnInterpreterPort | None = None,
        companion_decision_port: object | None = None,
    ) -> None:
        self.orchestrator = orchestrator
        self.companion_decision_port = companion_decision_port
        self.discover_runtime_ports = discover_runtime_ports
        self.current_turn_interpreter = current_turn_interpreter or (
            _runtime_current_turn_interpreter(orchestrator)
            if discover_runtime_ports and companion_decision_port is None
            and not os.environ.get('OLIVIA_JEV_DECISION_URL', '').strip() else None
        )
        runtime_reviewer, runtime_rewriter = (
            create_model_quality_ports(orchestrator)
            if discover_runtime_ports
            else (None, None)
        )
        self.reviewer = (
            runtime_reviewer
            if isinstance(reviewer, NullReviewer)
            and runtime_reviewer is not None
            else reviewer
        )
        self.rewriter = (
            runtime_rewriter
            if isinstance(rewriter, UnavailableRewriter)
            and runtime_rewriter is not None
            else rewriter
        )

    async def run(self, request: object, context: ReplyContext) -> PipelineResult:
        if not isinstance(context, ReplyContext):
            raise TypeError("ReplyContext is required")
        sticker_choices = allowed_stickers(context.private_behavior)
        if context.mode is ReplyMode.TEXT_LETTER:
            sticker_choices = weighted_candidates(
                sticker_choices, context.sticker_history, limit=32,
            )
        sticker_note = (LETTER_PRESENTATION_INSTRUCTION + '\n' + selection_instruction(sticker_choices)) if context.mode is ReplyMode.TEXT_LETTER else ""
        generation_note = sticker_note
        chat_metadata = None
        if context.mode is ReplyMode.FUTURE_IM:
            from runtime.personal_chat.presentation import CURRENT, INSTRUCTION
            chat_metadata = CURRENT.get()
            if CURRENT.get() is not None:
                if CURRENT.get().get('structured'):
                    from runtime.personal_chat.decision import INSTRUCTION as DECISION_INSTRUCTION
                    generation_note = DECISION_INSTRUCTION
                else:
                    generation_note = INSTRUCTION
        original_budget = request.max_input_chars if isinstance(request, ReplyRequest) else 0
        companion_enabled = (context.mode in {ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM}
            and not (chat_metadata or {}).get('proactive')
            and (self.companion_decision_port is not None or self.discover_runtime_ports
                 and os.environ.get('OLIVIA_JEV_DECISION_URL', '').strip()))
        generation_request = request
        # The decision projection is measured after classification. An arbitrary
        # worst-case reserve rejects otherwise valid persona/context inputs.
        if isinstance(request, ReplyRequest) and request.messages is None and original_budget > len(generation_note) + 1000:
            generation_request = replace(request, max_input_chars=original_budget-len(generation_note)-2)
        try:
            life_fragments = None
            if isinstance(generation_request, ReplyRequest) and generation_request.messages is None:
                adapter = getattr(getattr(self.orchestrator, 'gateway', None), 'adapter', None)
                select_life = getattr(adapter, 'prepare_daily_life_fragments', None)
                if callable(select_life) and getattr(adapter, 'daily_life', None) is not None:
                    from .world_context_selection import WorldSelectionError
                    try:
                        life_fragments = await select_life(generation_request.content or '', now=context.trusted_time.instant)
                    except WorldSelectionError as error:
                        return PipelineResult(generation_request.request_id, ReplyState.FAILED,
                                              error_code=str(error), retryable=True)
            preparation = _prepare_generation_request(
                generation_request,
                context,
                self.orchestrator,
                life_fragments=life_fragments,
            )
        except _PersonaNotReadyError:
            return PipelineResult(
                request.request_id if isinstance(request, ReplyRequest) else "",
                ReplyState.FAILED,
                error_code=_PERSONA_NOT_READY,
                retryable=False,
            )
        except _RecallBudgetExceeded as error:
            return PipelineResult(
                request.request_id if isinstance(request, ReplyRequest) else '',
                ReplyState.FAILED, error_code=error.code, retryable=False,
            )
        except PromptBudgetExceeded:
            return PipelineResult(getattr(request, 'request_id', ''), ReplyState.FAILED,
                error_code='JEV_CONTEXT_BUDGET_EXCEEDED' if companion_enabled else 'INPUT_TOO_LONG')
        prepared = preparation.request
        user_text = getattr(request, 'content', None)
        received = getattr(request, 'received_user_text', None)
        if received is not None:
            user_text = received
        if chat_metadata is not None:
            user_text = chat_metadata.get('raw_user_text')
        interpret_turn = context.mode in {ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM}
        if chat_metadata is not None and (chat_metadata.get('proactive') or user_text == ''):
            interpret_turn = False
        companion_port = self.companion_decision_port
        if interpret_turn and self.discover_runtime_ports and companion_port is None:
            try:
                from .companion_runtime import configured_port
                companion_port = configured_port()
            except ValueError:
                return PipelineResult(getattr(request, 'request_id', ''), ReplyState.FAILED,
                                      error_code='JEV_CONFIG_INVALID')
        use_companion = interpret_turn and companion_port is not None
        companion_decision = companion_timing = companion_delivery = None
        proactive_decision = None
        adapter = getattr(getattr(self.orchestrator, 'gateway', None), 'adapter', None)
        appraise = getattr(adapter, 'prepare_character_emotion', None)

        async def interpret_input():
            if self.current_turn_interpreter is not None and interpret_turn and not use_companion:
                if not isinstance(prepared, ReplyRequest) or not isinstance(user_text, str):
                    raise ValueError('current user input unavailable')
                return await self.current_turn_interpreter.interpret(user_text)

        async def appraise_input():
            if callable(appraise) and context.mode in {ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM,
                                                      ReplyMode.VOICE_REPLY, ReplyMode.SPOKEN_VIDEO}:
                try:
                    proactive = chat_metadata is not None and chat_metadata.get('proactive')
                    return await appraise(None if proactive or user_text == '' else user_text,
                                          now=context.trusted_time.instant)
                except Exception:
                    return None  # Optional subjective state cannot block received text.

        # Separate contracts, concurrent work. Cancellation also cancels both
        # children; no detached appraisal keeps running against an obsolete draft.
        interpretation, emotion_view = await asyncio.gather(
            interpret_input(), appraise_input(), return_exceptions=True)
        if isinstance(interpretation, BaseException):
            if not isinstance(interpretation, Exception):
                raise interpretation
            return PipelineResult(getattr(request, 'request_id', ''), ReplyState.FAILED,
                                  error_code='CURRENT_TURN_INTERPRETATION_FAILED')
        if self.current_turn_interpreter is not None and interpret_turn and not use_companion:
            try:
                messages = projection_messages(
                    _generation_messages(prepared), user_text, interpretation,
                )
                if sum(len(str(message.get("content", ""))) for message in messages) > prepared.max_input_chars:
                    raise ValueError("interpretation exceeds request budget")
                prepared = replace(prepared, messages=messages)
            except Exception:
                return PipelineResult(
                    request.request_id if isinstance(request, ReplyRequest) else "",
                    ReplyState.FAILED, error_code="CURRENT_TURN_INTERPRETATION_FAILED",
                )
        adopted = {}
        if isinstance(prepared, ReplyRequest) and prepared.messages:
            from runtime.memory.history_selection import select_history_messages as prepare_recall_messages
            adapter = getattr(getattr(self.orchestrator, 'gateway', None), 'adapter', None)
            gateway = getattr(adapter, 'gateway', None)
            current_sources = getattr(adapter, '_memory_source_exclusions', lambda: ())()
            messages = await prepare_recall_messages(prepared.messages, gateway,
                max_input_chars=prepared.max_input_chars, request_id=prepared.request_id,
                memory_builder=getattr(adapter, 'memory_prompt_builder', None),
                as_of=context.trusted_time.instant,
                exclude_source_ids=current_sources, current_source_ids=current_sources,
                current_user_text=user_text, persona_snapshot=preparation.persona_snapshot,
                persona_mode=persona_mode_for_reply_mode(context.mode),
                persona_development=(preparation.adopted_world or {}).get('character_development'))
            prepared = replace(prepared, messages=messages)
        if isinstance(prepared, ReplyRequest) and prepared.messages:
            from .fact_attribution import prepare_dialogue_messages
            prepared = replace(prepared, messages=prepare_dialogue_messages(
                prepared.messages, max_input_chars=prepared.max_input_chars))
        if use_companion:
            from .companion_runtime import prepare_decision, delivery_for, project_decision, CompanionRuntimeError, TURN_CONTEXT
            metadata = chat_metadata if chat_metadata is not None else (TURN_CONTEXT.get() or {})
            kinds = metadata.get('semantic_kinds', ['text'])
            try:
                if isinstance(prepared, ReplyRequest) and prepared.messages is None:
                    prepared = replace(prepared, messages=prepared.normalized_messages())
                decision = await prepare_decision(companion_port, _generation_messages(prepared), user_text,
                    source_id=metadata.get('received_source_id') or getattr(request, 'request_id', ''),
                    input_revision=metadata.get('input_revision', 0),
                    as_of=context.trusted_time.instant.isoformat(), kinds=kinds,
                    cached=metadata.get('companion_decision'))
                companion_decision = decision.record()
                save_decision = metadata.get('save_companion_decision')
                if callable(save_decision):
                    try:
                        await save_decision(companion_decision)
                    except Exception:
                        raise CompanionRuntimeError('JEV_DECISION_NOT_SAVED') from None
                companion_timing, companion_delivery = delivery_for(decision, kinds=kinds)
                if companion_timing in {'wait_user', 'defer', 'no_reply'}:
                    return PipelineResult(getattr(request, 'request_id', ''), ReplyState.COMPLETED,
                        companion_decision=companion_decision, companion_timing=companion_timing)
                prepared = replace(prepared, messages=project_decision(_generation_messages(prepared), decision,
                    max_input_chars=original_budget-len(generation_note)-2,
                    delivery=('letter_image' if context.mode is ReplyMode.TEXT_LETTER
                              and companion_delivery == 'image' else companion_delivery)),
                    max_input_chars=original_budget-len(generation_note)-2)
            except CompanionRuntimeError as error:
                return PipelineResult(getattr(request, 'request_id', ''), ReplyState.FAILED,
                    error_code=str(error), companion_decision=companion_decision)
        if isinstance(prepared, ReplyRequest) and prepared.messages:
            from .character_emotion_context import project_emotion
            prepared = replace(prepared, messages=project_emotion(prepared.messages, emotion_view,
                max_input_chars=prepared.max_input_chars, adopted=adopted))
        proactive_decide = (chat_metadata or {}).get('proactive_decide')
        if (chat_metadata or {}).get('proactive') and callable(proactive_decide):
            from .proactive_runtime import project_decision, record_decision
            from .companion_runtime import CompanionRuntimeError
            try:
                world = (_assembled_life_projection(_generation_messages(prepared))
                         if preparation.adopted_world is not None else None)
                decision = await proactive_decide(_generation_messages(prepared), world, adopted.get('emotion'))
                proactive_decision = record_decision(decision)
                if decision.decision['action'] == 'defer':
                    return PipelineResult(getattr(request, 'request_id', ''), ReplyState.COMPLETED,
                                          proactive_decision=proactive_decision)
                prepared = replace(prepared, messages=project_decision(
                    _generation_messages(prepared), decision,
                    max_input_chars=original_budget-len(generation_note)-2))
            except CompanionRuntimeError as error:
                return PipelineResult(getattr(request, 'request_id', ''), ReplyState.FAILED,
                                      error_code=str(error), proactive_decision=proactive_decision)
        if generation_note and isinstance(prepared, ReplyRequest) and prepared.messages:
            # Finalize the delivery contract after dialogue/recall projection.
            # The current user input stays last; evidence cannot become the
            # last instruction defining what the model is supposed to output.
            from .fact_attribution import finalize_reply_messages
            try:
                messages = finalize_reply_messages(prepared.messages, generation_note,
                                                   max_input_chars=original_budget)
                from runtime.personal_chat.decision import INSTRUCTION as CHAT_RULES
                if (chat_metadata or {}).get('structured') and generation_note == CHAT_RULES:
                    from .fact_attribution import cache_output_rules
                    messages = cache_output_rules(messages, generation_note)
            except ValueError:
                return PipelineResult(prepared.request_id, ReplyState.FAILED,
                                      error_code='INPUT_TOO_LONG', retryable=False)
            prepared = replace(prepared, messages=messages, max_input_chars=original_budget)
        from .character_emotion_context import freeze_expression_context
        # Local assembly establishes provenance; later recall may legitimately
        # replace duplicated notes with source references. Freeze that final
        # retained projection, never revive a block omitted by local assembly.
        world = (_assembled_life_projection(_generation_messages(prepared))
                 if preparation.adopted_world is not None else None)
        expression_context = freeze_expression_context(getattr(request, 'request_id', ''),
            context.trusted_time.instant, world=world, emotion=adopted.get('emotion'))
        candidate = await self.orchestrator.run(prepared)
        if candidate.state is not ReplyState.COMPLETED:
            return PipelineResult(
                candidate.request_id,
                candidate.state,
                error_code=candidate.error_code,
                retryable=candidate.retryable,
            )
        clean_text, sticker_id = split_selection(candidate.text, sticker_choices) if sticker_note else (candidate.text, None)
        clean_text, signature = split_signature(clean_text) if sticker_note else (clean_text, None)
        if not clean_text.strip():
            return PipelineResult(candidate.request_id, ReplyState.FAILED, error_code="PROVIDER_PROTOCOL")
        quality = None
        if not isinstance(self.reviewer, NullReviewer) and context.mode in {ReplyMode.TEXT_LETTER, ReplyMode.FUTURE_IM}:
            envelope = None
            review_text = clean_text
            if chat_metadata is not None and chat_metadata.get('structured'):
                try:
                    from runtime.personal_chat.decision import decode
                    now = datetime.fromisoformat(chat_metadata['decision_now']).timestamp()
                    options = dict(user=user_text, now=now, proactive=bool(chat_metadata.get('proactive')))
                    decision = decode(clean_text, **options)
                    fenced = re.fullmatch(r'\s*```(?:json)?\s*\n(.*?)\n```\s*', clean_text, re.DOTALL | re.IGNORECASE)
                    envelope = json.loads(fenced.group(1) if fenced else clean_text)
                    review_text = decision['text']
                except (ValueError, TypeError, KeyError):
                    return PipelineResult(candidate.request_id, ReplyState.FAILED, error_code='PERSONAL_CHAT_DECISION_INVALID')
            # A validated proactive skip has no outgoing text to review.
            if envelope is None or not decision['skip']:
                # Delivery JSON / sticker instructions are not prose rewrite instructions.
                review_messages = tuple(m for m in _generation_messages(prepared)
                    if not (m.get('role') == 'system' and m.get('content') == generation_note))
                if envelope is not None:
                    review_messages = [dict(m) for m in review_messages]
                    for message in reversed(review_messages):
                        if message.get('role') == 'user':
                            message['content'] = user_text
                            break
                    evidence = []
                    observation = chat_metadata.get('incoming_observation_context')
                    if observation:
                        evidence.append('<evidence_summary>' + json.dumps({
                            'text': observation, 'evidence_kind': 'incoming_observation',
                            'untrusted': True}, ensure_ascii=False).replace('<', r'\u003c') + '</evidence_summary>')
                    plan = {key: value for key, value in envelope.items() if key in decision and key != 'text'}
                    evidence.append('<reply_delivery_plan>' + json.dumps({
                        'text': json.dumps(plan, ensure_ascii=False),
                        'evidence_kind': 'planned_delivery', 'untrusted': True,
                        'meaning': 'Frozen delivery plan, not an occurred event; rewrite only the reply text.'
                    }, ensure_ascii=False).replace('<', r'\u003c') + '</reply_delivery_plan>')
                    # Keep current user last for both review and prose-only rewrite.
                    position = next((i for i in range(len(review_messages)-1, -1, -1)
                                     if review_messages[i].get('role') == 'user'), len(review_messages))
                    review_messages.insert(position, {'role': 'system', 'content': '\n'.join(evidence)})
                    review_messages = tuple(review_messages)
                if not review_messages and isinstance(user_text, str):
                    review_messages = ({'role': 'user', 'content': user_text},)
                try:
                    from runtime.persona.persona_selection import snapshot_for_messages
                    from .reply_model_quality import using_persona_snapshot
                    with using_persona_snapshot(snapshot_for_messages(preparation.persona_snapshot, review_messages)):
                        quality = await asyncio.to_thread(run_reply_quality_gate, review_text, context,
                            reviewer=self.reviewer, rewriter=self.rewriter,
                            generation_messages=review_messages, trusted_evidence=preparation.trusted_evidence)
                except Exception:
                    return PipelineResult(candidate.request_id, ReplyState.FAILED, error_code='REVIEW_FAILED',
                                          quality_status='blocked', reviewer_calls=1)
                if not quality.accepted:
                    return PipelineResult(candidate.request_id, ReplyState.FAILED,
                        error_code=quality.error_code or 'REPLY_QUALITY_BLOCKED', quality_status=quality.status.value,
                        violation_codes=quality.violation_codes, reviewer_calls=quality.reviewer_calls,
                        rewrite_calls=quality.rewrite_calls)
                clean_text = quality.text
                if envelope is not None:
                    # Preserve wire-format timestamps, preferences, and media selections.
                    # decode's normalized timestamps are not the generation JSON contract.
                    envelope['text'] = clean_text
                    clean_text = json.dumps(envelope, ensure_ascii=False)
                    try:
                        decode(clean_text, **options)
                    except ValueError:
                        return PipelineResult(candidate.request_id, ReplyState.FAILED,
                            error_code='PERSONAL_CHAT_DECISION_INVALID', quality_status='blocked',
                            reviewer_calls=quality.reviewer_calls, rewrite_calls=quality.rewrite_calls)
        shadow = None
        if (os.environ.get('OLIVIA_SEMANTIC_SHADOW_URL', '').strip()
                and not use_companion and chat_metadata is not None and not chat_metadata.get('proactive') and user_text):
            from runtime.reply.semantic_shadow import observe
            shadow = asyncio.create_task(observe(_generation_messages(prepared), user_text,
                                                chat_metadata.get('semantic_kinds', ['text'])))
        return PipelineResult(
            candidate.request_id,
            ReplyState.COMPLETED,
            text=clean_text,
            sticker_id=sticker_id,
            signature=signature,
            quality_status=quality.status.value if quality else "not_checked",
            violation_codes=quality.violation_codes if quality else (),
            reviewer_calls=quality.reviewer_calls if quality else 0,
            rewrite_calls=quality.rewrite_calls if quality else 0,
            semantic_shadow_task=shadow,
            expression_context=expression_context,
            companion_decision=companion_decision,
            companion_timing=companion_timing,
            companion_delivery=companion_delivery,
            proactive_decision=proactive_decision,
        )



def _prepare_generation_request(
    request: object,
    context: ReplyContext,
    orchestrator: OrchestratorPort,
    *, life_fragments=None,
) -> _PreparedGeneration:
    """Attach Persona messages before provider generation when the local bridge is used."""

    if (
        not isinstance(request, ReplyRequest)
        or request.messages is not None
        or not isinstance(request.content, str)
        or not request.content.strip()
    ):
        return _PreparedGeneration(request)

    bridge = getattr(orchestrator, "gateway", None)
    adapter = getattr(bridge, "adapter", None)
    config = getattr(adapter, "config", None)
    provider_name = str(getattr(config, "provider", "none")).strip().lower()
    if (
        adapter is None
        or not getattr(config, "persona_v2_enabled", False)
        or provider_name in {"", "none", "disabled", "unconfigured"}
    ):
        return _PreparedGeneration(request)

    persona_path = getattr(adapter, "persona_v2_path", None)
    memory_builder = getattr(adapter, "memory_prompt_builder", None)
    if persona_path is None or memory_builder is None:
        raise ValueError("persona generation boundary is unavailable")

    loaded = load_persona(persona_path)
    if loaded.snapshot.status != "READY":
        raise _PersonaNotReadyError(_PERSONA_NOT_READY)
    messages, evidence = assemble_reply_messages(adapter, loaded.snapshot, context,
        request.content, max_input_chars=request.max_input_chars, life_fragments=life_fragments)
    if (life_fragments is not None and any(f.fragment_id == 'linli.daily-life' for f in life_fragments)
            and _assembled_life_projection(messages) is None):
        raise _WorldSelectionBudgetExceeded()
    return _PreparedGeneration(replace(request, content=None, messages=messages), evidence,
                               _assembled_life_projection(messages), loaded.snapshot)


def _assembled_life_projection(messages):
    """Read the bounded block from our own assembly, never a caller's prompt.

    Used first at the trusted local assembly boundary, then to capture its final
    projection. User messages (including quoted lookalike tags) cannot seed it.
    """
    try:
        for message in messages:
            if message.get('role') != 'system':
                continue
            for match in re.finditer(r'<evidence_summary>\s*(.*?)\s*</evidence_summary>',
                                     message.get('content', ''), re.DOTALL):
                wrapper = json.loads(match.group(1))
                if wrapper.get('fragment_id') == 'linli.daily-life':
                    value = json.loads(wrapper['text'])
                    if isinstance(value, dict) and value.get('kind') == 'character_life_reference':
                        return value
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        pass
    return None


def assemble_reply_messages(adapter, snapshot, context, content, *, max_input_chars, user_input=None, life_fragments=None):
    """One memory/world assembly for every user-facing reply and media plan."""
    from .fact_attribution import prepare_dialogue_messages
    messages, evidence, assembly_limit = _assemble_reply_evidence(adapter, snapshot, context, content,
        max_input_chars=max_input_chars, user_input=user_input, life_fragments=life_fragments)
    return prepare_dialogue_messages(messages, max_input_chars=assembly_limit), evidence


def _assemble_reply_evidence(adapter, snapshot, context, content, *, max_input_chars, user_input=None, life_fragments=None):
    from inspect import signature
    life = getattr(adapter, 'daily_life_fragments', None) if life_fragments is None else None
    recent = getattr(adapter, 'recent_letter_fragments', None)
    if callable(recent):
        try:
            accepts_now = 'now' in signature(recent).parameters
        except (TypeError, ValueError):
            accepts_now = False
        recent = recent(content, now=context.trusted_time.instant) if accepts_now else recent(content)
    else:
        recent = ()
    if callable(life):
        # Older adapters expose only content; the local adapter accepts this
        # request's frozen recent window instead of reading the mailbox again.
        try:
            parameters = signature(life).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs = {}
        if 'recent_fragments' in parameters:
            kwargs['recent_fragments'] = recent
        if 'now' in parameters:
            kwargs['now'] = context.trusted_time.instant
        life = life(content, **kwargs)
    else:
        life = () if life_fragments is None else life_fragments
    # All modes retain the same minimum turn context, including when the
    # interpreter is disabled. Reserve the frozen current state as well.
    reserve = 512
    for fragment in life:
        if fragment.fragment_id == 'linli.daily-life':
            try:
                current = json.loads(fragment.text).get('current')
                if isinstance(current, dict):
                    reserve += len(json.dumps(current, ensure_ascii=False).replace('<', r'\u003c').replace('>', r'\u003e')) + 100
            except (ValueError, AttributeError, TypeError):
                pass
    assembly_limit = max(1, max_input_chars - reserve)
    options = dict(snapshot=snapshot, context=context,
        user_input=content if user_input is None else user_input,
        max_units=assembly_limit, evidence_summaries=life,
        relationship_expression_enabled=snapshot.status == 'READY', selected_declaration_ids=())
    limit = getattr(adapter, '_memory_context_limit', None)
    disabled = callable(limit) and limit() == 0
    from runtime.memory.history_continuity import plan_history_query, HistoryQuery
    exclusions = getattr(adapter, '_memory_source_exclusions', lambda: ())()
    query_plan = HistoryQuery(content) if disabled else plan_history_query(content, recent, excluded=exclusions)
    hint = query_plan.fragment()
    if hint is not None:
        recent = (*recent, hint)
    from runtime.reply.prompt_budget import PromptBudgetExceeded
    try:
        baseline = assemble_persona(history=recent, **options)
    except PromptBudgetExceeded as error:
        raise _RecallBudgetExceeded() from error
    available = max(0, assembly_limit - len(baseline.system_content) - len(baseline.user_content) - 256)
    if disabled:
        adapter._build_memory_prompt(content, max_chars=0)
        return baseline.to_messages(), TrustedReviewEvidence(), assembly_limit
    from runtime.diagnostics.recall_trace import begin, selection as record_selection
    begin(adapter.memory_prompt_builder, query_plan.mode)
    build_memory_prompt = getattr(adapter, "_build_memory_prompt", None)
    build_memory_prompt = build_memory_prompt if callable(build_memory_prompt) else adapter.memory_prompt_builder.build
    memory = build_memory_prompt(query_plan.query, max_chars=max(1, available))
    # One query, optionally grounded in delivered context. Capacity retries
    # only repack the same evidence; the archive tail is a bounded local read.
    from runtime.memory.memory_prompt import MemoryPromptBuilder
    from runtime.memory.memory_port import NullMemoryPort
    recall = getattr(memory, 'recall_result', None)
    renderer = MemoryPromptBuilder(NullMemoryPort(), conversation_memory=None,
        max_tokens=getattr(adapter.memory_prompt_builder, 'max_tokens', 300000),
        legacy_budget=available, conversation_budget=available)
    if recall is not None:
        from runtime.memory.history_continuity import add_history_tail
        recall = add_history_tail(adapter.memory_prompt_builder, recall, query_plan,
            now=context.trusted_time.instant, excluded=exclusions)
        from runtime.memory.recall_trace import deepen_recall
        builder = adapter.memory_prompt_builder
        if callable(getattr(builder, 'trace_sources', None)):
            exclusions = getattr(adapter, '_memory_source_exclusions', lambda: ())()
            recall = deepen_recall(builder, recall, query=content,
                source_ids=_life_source_ids(life), exclude_source_ids=exclusions)
        states = dict(recall.source_status)
        states['world'] = 'available' if context.world_state_available else 'unavailable'
        recall = replace(recall, source_status=tuple(states.items()))
        memory = renderer.render(recall, max_chars=available)
    while available > 0:
        if not getattr(memory, 'text', ''):
            break
        selection = _selected_history(memory)
        result = assemble_persona(history=(*selection.fragments, *recent), **options)
        included = result.budget_report.included_ids
        if 'history.memory.references' in included:
            if recall is not None:
                record_selection(recall, memory.references)
            return result.to_messages(), selection.trusted_evidence, assembly_limit
        available = available * 3 // 4
        if recall is None:
            break  # Legacy builders cannot be safely requeried during one generation.
        memory = renderer.render(recall, max_chars=available)
    if recall is not None:
        record_selection(recall, ())
        # Reserve failure/omission disclosure before optional reference blocks.
        # This is the same untrusted wrapper used by the persona assembler.
        minimum = renderer.minimum_recall_status(recall)
        payload = json.dumps({'untrusted': True, 'text': minimum}, ensure_ascii=False, separators=(',', ':'))
        payload = payload.replace('<', r'\u003c').replace('>', r'\u003e')
        disclosure = '<untrusted_history>\n' + payload + '\n</untrusted_history>\n'
        remaining = assembly_limit - len(disclosure)
        if remaining < 1:
            raise _RecallBudgetExceeded(_RecallBudgetExceeded.code)
        from runtime.reply.prompt_budget import PromptBudgetExceeded
        try:
            result = assemble_persona(history=recent, **{**options, 'max_units': remaining})
        except PromptBudgetExceeded as error:
            raise _RecallBudgetExceeded(_RecallBudgetExceeded.code) from error
        return ({'role': 'system', 'content': result.system_content + disclosure},
                {'role': 'user', 'content': result.user_content}), TrustedReviewEvidence(), assembly_limit
    return baseline.to_messages(), TrustedReviewEvidence(), assembly_limit


def _life_source_ids(fragments) -> tuple[str, ...]:
    """Read explicit event provenance from this request's already-frozen life view."""
    sources = []
    for fragment in fragments:
        if fragment.fragment_id != 'linli.daily-life':
            continue
        try:
            value = json.loads(fragment.text)
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        items = [value.get('current'), value.get('last_observation')]
        items.extend(value.get('previous_observations', []) if isinstance(value.get('previous_observations'), list) else [])
        items.extend(value.get('threads', []) if isinstance(value.get('threads'), list) else [])
        for item in items:
            source = item.get('source_id') if isinstance(item, dict) else None
            if isinstance(source, str) and source and not source.startswith('day:') and source not in sources:
                sources.append(source)
    return tuple(sources[:12])


def _selected_history(memory_context: object) -> _SelectedHistory:
    trusted_replies: list[TrustedCharacterReply] = []
    remaining = _CHARACTER_REPLY_HISTORY_LIMIT
    references = getattr(memory_context, "references", ())
    if isinstance(references, tuple):
        for reference in references:
            fragment = _character_reply_fragment(reference, remaining=remaining)
            if fragment is None:
                continue
            trusted_replies.append(
                TrustedCharacterReply(
                    fragment.fragment_id,
                    fragment.text[len(_CHARACTER_REPLY_PREFIX) :],
                )
            )
            remaining -= len(fragment.text)
            if remaining <= len(_CHARACTER_REPLY_PREFIX):
                break
    memory_text = getattr(memory_context, "text", "")
    memory_reference = (
        (UntrustedFragment("memory.references", memory_text),)
        if isinstance(memory_text, str) and memory_text
        else ()
    )
    return _SelectedHistory(
        # Generation sees only the source-bearing group, including its paired
        # user claim and time. A naked duplicate can look like present self-report.
        # The bounded reviewer projection remains separate and is usable only
        # when the corresponding memory block survives final prompt assembly.
        memory_reference,
        TrustedReviewEvidence(tuple(trusted_replies)),
    )


def _character_reply_fragment(
    reference: object,
    *,
    remaining: int,
) -> UntrustedFragment | None:
    if not isinstance(reference, MemoryRecord):
        return None
    source_id = reference.provenance.get("source_record_id")
    if (
        reference.domain != CONVERSATION_MEMORY
        or not isinstance(source_id, str)
        or not source_id.startswith("history:")
        or reference.metadata.get("canonical") is not True
        or reference.metadata.get("history_actor") != "linli"
    ):
        return None
    text = reference.text.strip()
    available = remaining - len(_CHARACTER_REPLY_PREFIX)
    if (
        available <= 0
        or not text
        or len(text) > available
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
    ):
        return None
    digest = hashlib.sha256(
        f"{source_id}\0{reference.memory_id}".encode("utf-8")
    ).hexdigest()
    return UntrustedFragment(
        f"character_reply.{digest}",
        f"{_CHARACTER_REPLY_PREFIX}{text}",
    )


def _generation_messages(
    request: object,
) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(request, ReplyRequest) or request.messages is None:
        return ()
    return tuple(dict(message) for message in request.messages)
