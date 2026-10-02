"""A budget-exhausted upstream result must never become a successful WAV."""
import json
from types import SimpleNamespace

import pytest
import torch

from tts import external_breeze_worker as worker


@pytest.mark.parametrize('cached', [False, True])
@pytest.mark.parametrize('progress_counts', [(64,), (64, 12), (12, 64), (12,), (12, 12)])
def test_budget_exhaustion_rejects_entire_performance(tmp_path, monkeypatch, cached, progress_counts):
    bundle = SimpleNamespace(codec='codec', model=object())
    def decode(*args):
        return torch.ones(1, 1, 1920)
    runtime = SimpleNamespace(decode_codes=decode,
        comfy_audio_to_tensor=lambda audio: ('reference', 24000),
        encode_reference_audio=lambda *args: 'reference-codes')
    loader = SimpleNamespace(HYBRID_LABEL='hybrid', load_breeze_bundle=lambda *args: bundle)
    counts = iter(progress_counts)
    def generate(bundle, **kwargs):
        kwargs['progress_callback'](next(counts), 64)
        return {'sample_rate': 24000, 'waveform': runtime.decode_codes('codec', torch.zeros(1, 16))}
    monkeypatch.setattr(worker, '_load_package', lambda *args: (loader, SimpleNamespace(_generate_audio=generate), runtime))
    monkeypatch.setattr(worker, '_read_reference_audio', lambda *args: {})
    monkeypatch.setattr(worker, '_plan_audio_chunks', lambda *args: ['合成测试。'] * len(progress_counts))
    reference = tmp_path / 'reference.wav'
    reference.write_bytes(b'fixture')
    request = dict(runtime_root=str(tmp_path), model_dir=str(tmp_path),
        reference_audio=str(reference), reference_text='参考', text='合成测试。', instruction='',
        audio_only_unbounded=True)
    status = tmp_path / 'status.json'
    output = tmp_path / 'speech.wav'
    if 64 in progress_counts:
        with pytest.raises(RuntimeError, match='^BREEZE_GENERATION_INCOMPLETE$'):
            worker._synthesize_impl(request, output, status, cache={} if cached else None)
        assert not output.exists()
    else:
        worker._synthesize_impl(request, output, status, cache={} if cached else None)
        assert output.exists()
    result = json.loads(status.read_text(encoding='utf-8'))
    if 64 in progress_counts:
        assert result['status'] == 'failed'
        assert result['error_code'] == 'BREEZE_GENERATION_INCOMPLETE'
        assert result['phase'] == 'generation'
        assert result.get('audio_started', False) is False
        assert result['limit_reached'] is True
    else:
        assert result['status'] == 'completed'
        assert result['limit_reached'] is False
    # Restore upstream hooks even when rejecting a result in a resident worker.
    assert runtime.decode_codes is decode
