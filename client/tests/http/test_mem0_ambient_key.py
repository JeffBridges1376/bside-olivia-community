def test_mem0_sdk_never_sends_ambient_cloud_key_to_the_relay(monkeypatch):
    from types import SimpleNamespace
    import httpx
    from openai import OpenAI
    from runtime.memory import mem0_memory

    monkeypatch.setenv('OPENAI_API_KEY', 'ambient-secret')
    received = []
    class Memory:
        @staticmethod
        def from_config(config):
            llm = config['llm']['config']
            assert llm['api_key'] != ''  # Prevent Mem0's `key or getenv` fallback.
            return SimpleNamespace(llm=SimpleNamespace(client=OpenAI(api_key=llm['api_key'], base_url=llm['openai_base_url'])))
    monkeypatch.setattr(mem0_memory, '_load_product_mem0_module', lambda: SimpleNamespace(Memory=Memory))
    backend = mem0_memory._default_factory({'llm': {'provider': 'openai', 'config': {'api_key': '', 'model': 'qwen3.7-flash', 'openai_base_url': 'http://127.0.0.1:19001/v1'}}})
    client = backend.llm.client
    hooks = client._client.event_hooks
    client._client.close()
    def respond(request):
        received.append(request)
        return httpx.Response(200, json={'id': 'fixture', 'object': 'chat.completion', 'created': 0, 'model': 'local', 'choices': [{'index': 0, 'finish_reason': 'stop', 'message': {'role': 'assistant', 'content': 'OK'}}]})
    client._client = httpx.Client(transport=httpx.MockTransport(respond), event_hooks=hooks)
    try:
        client.chat.completions.create(model='local', messages=[{'role': 'user', 'content': 'synthetic memory'}])
        assert received[0].url.host == '127.0.0.1'
        assert received[0].headers['authorization'] == 'Bearer olivia-no-key'
        assert 'ambient-secret' not in str(received[0].headers)
    finally:
        client.close()
