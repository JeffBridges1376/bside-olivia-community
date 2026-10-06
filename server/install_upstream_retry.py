"""Prepare bounded cloud LLM retries; never deploy, restart, or change a database."""
import argparse
import hashlib
from pathlib import Path

BASELINE = 'bc235ee74b7f423ec017ea7bf940302b8b35d565a1385d06b87fe282a366b8ec'
RESPONSES_BASELINE = '75cd80baadb67d34419b4bebe4f60819a4b39273959267d1d322dbe1174b675a'


def prepare(root):
    source = (root / 'qwen/async_relay.py').read_bytes().decode('utf-8').replace('\r\n', '\n')
    if hashlib.sha256(source.encode()).hexdigest() != BASELINE:
        raise ValueError('Cloud relay changed; review a fresh baseline')
    responses = (root / 'qwen/responses_bridge.py').read_bytes().decode('utf-8').replace('\r\n', '\n')
    if hashlib.sha256(responses.encode()).hexdigest() != RESPONSES_BASELINE:
        raise ValueError('Cloud Responses bridge changed; review a fresh baseline')

    def change(before, after):
        nonlocal source
        if source.count(before) != 1:
            raise ValueError('Unexpected cloud relay source')
        source = source.replace(before, after, 1)

    change('import httpx\n', 'import httpx\nfrom .upstream_retry import RetryClient\n')
    change("            phase = 'upstream_dispatch'\n",
           "            phase = 'upstream_dispatch'\n"
           '            upstream_client = RetryClient(self.client, trace, lambda: db(authenticate, key))\n')
    change('output = await responses_complete(self.client,', 'output = await responses_complete(upstream_client,')
    change("async with self.client.stream('POST', upstream_base.rstrip('/') + '/chat/completions',",
           "async with upstream_client.stream('POST', upstream_base.rstrip('/') + '/chat/completions',")
    change('                    if status in (400, 401, 403, 404, 413, 422, 429):',
           "                    if status in (400, 401, 403, 404, 413, 422, 429) or trace.get('upstream_failure_confirmed'):")
    change("                    return await fail('upstream_rate_limited' if status == 429 else\n",
           "                    return await fail('upstream_rejected' if trace.get('upstream_failure_confirmed') else\n"
           "                                      'upstream_rate_limited' if status == 429 else\n")
    change('                        if result.status_code in (400, 401, 403, 404, 413, 422, 429):',
           "                        if result.status_code in (400, 401, 403, 404, 413, 422, 429) or trace.get('upstream_failure_confirmed'):")
    change("                        return await fail('upstream_rate_limited' if result.status_code == 429 else\n",
           "                        return await fail('upstream_rejected' if trace.get('upstream_failure_confirmed') else\n"
           "                                                'upstream_rate_limited' if result.status_code == 429 else\n")
    change('            if started:\n',
           "            if trace.get('upstream_failure_confirmed') and not started:\n"
           "                await fail('upstream_rejected', 502)\n"
           '            elif started:\n')
    change('                if record is not None and not dispatched:\n',
           "                if record is not None and (not dispatched or trace.get('upstream_failure_confirmed')):\n")
    responses = responses.replace('    # A single paid request, with no hidden provider retries or persisted state.',
        '    # The caller owns bounded retries of explicit pre-output failures.')
    sources = {'async_relay.py': source, 'responses_bridge.py': responses,
               'upstream_retry.py': Path(__file__).with_name('upstream_retry.py').read_text(encoding='utf-8')}
    installed = root / 'qwen/upstream_retry.py'
    if installed.exists() and installed.read_text(encoding='utf-8') != sources['upstream_retry.py']:
        raise ValueError('Existing cloud retry helper changed; preserve it for review')
    for name, text in sources.items():
        compile(text, name, 'exec')
    return sources


def apply(root):
    for name, source in prepare(root).items():
        (root / 'qwen' / name).write_bytes(source.encode())


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    apply(parser.parse_args().root)
