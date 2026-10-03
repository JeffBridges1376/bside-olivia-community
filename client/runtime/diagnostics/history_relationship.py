"""Finite assessment failure metadata for durable support evidence."""

from collections.abc import Mapping

from runtime.reply.companion_decision import ERROR_CODES as JEV_ERROR_CODES


HISTORY_CAUSE_CODES = frozenset({
    'UNKNOWN', 'INPUT_TOO_LONG', 'INVALID_INPUT', 'INVALID_MESSAGES',
    'INVALID_MESSAGE', 'INVALID_ROLE', 'INVALID_MESSAGE_CONTENT', 'RESULT_INVALID',
    'PROVIDER_QUOTA_EXHAUSTED', 'PROVIDER_TIMEOUT', 'PROVIDER_PROTOCOL',
    'PROVIDER_UNAVAILABLE', 'PROVIDER_RETRYABLE', 'PROVIDER_REJECTED',
    'PROVIDER_AUTH_FAILED', 'PROVIDER_USAGE_PENDING', 'PROVIDER_REQUEST_DUPLICATE',
    *JEV_ERROR_CODES,
})
_STAGES = frozenset({'prepare', 'gateway', 'jev', 'result_validation', 'commit'})
_EXCEPTIONS = frozenset({
    'InvalidGatewayInput', 'GatewayError', 'ProviderUnavailable', 'ProviderTimeout',
    'ProviderRetryableError', 'ProviderRejected', 'ProviderProtocolError',
    'ProviderEmptyResponse', 'ValueError', 'TypeError', 'KeyError', 'AttributeError',
    'RuntimeError', 'JSONDecodeError', 'CompanionDecisionError', 'OTHER',
})
_COUNTS = frozenset({
    'input_chars', 'max_input_chars', 'input_bytes', 'max_input_bytes',
    'exchange_count', 'batch_id',
})


def project_history_failure_context(source):
    """Reject arbitrary exception text, content, identifiers, and malformed counts."""
    if not isinstance(source, Mapping):
        return {}
    result = {}
    for key, values in (
        ('cause_code', HISTORY_CAUSE_CODES), ('failure_stage', _STAGES),
        ('exception_type', _EXCEPTIONS),
    ):
        value = source.get(key)
        if isinstance(value, str) and value in values:
            result[key] = value
    for key in _COUNTS:
        value = source.get(key)
        if type(value) is int and 0 <= value <= 1_000_000_000:
            result[key] = value
    return result
