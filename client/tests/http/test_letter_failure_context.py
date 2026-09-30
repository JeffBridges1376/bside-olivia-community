

def test_protocol_detail_reaches_letter_failure_context():
    from llm_gateway import ProviderProtocolError
    from runtime.diagnostics.failure_context import letter_failure_context
    try:
        try:
            raise ProviderProtocolError('output_truncated')
        except ProviderProtocolError:
            raise RuntimeError('LLM_PROTOCOL_ERROR') from None
    except RuntimeError as exc:
        assert letter_failure_context(exc)['failure_detail'] == 'output_truncated'
