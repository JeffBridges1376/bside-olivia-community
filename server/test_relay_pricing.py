import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import install_relay_pricing as installer


MODELS = '''from decimal import Decimal
OFFICIAL_VERSION = 'olivia-official-20261005-fx67269'
DISCOUNT_VERSION = 'olivia-official-20261005-sale80-fx67269'
OFFICIAL_DIVISOR = 10000
NEW_MODELS = {
    'claude-sonnet-5-5': ('Sonnet', 'anthropic', '13.4538', '67.269'),
    'gemini-3.8-flash': ('Gemini', 'google', '5.045175', '25.225875'),
    'gpt-6-luna': ('Luna', 'openai', '.67269', '3.36345'),
    'qwen3.8-max': ('Max', 'qwen', '12', '36'),
}
def retail_yuan_rates(model):
    row = NEW_MODELS.get(model)
    if row is None:
        raise ValueError('model_not_priced')
    discount = Decimal('1') if row[1] == 'qwen' else Decimal('.8')
    return Decimal(row[2]) * discount, Decimal(row[3]) * discount
def retail_rates(model):
    ir, ort = retail_yuan_rates(model)
    version = OFFICIAL_VERSION if NEW_MODELS[model][1] == 'qwen' else DISCOUNT_VERSION
    return version, int(ir * 100 * OFFICIAL_DIVISOR), int(ort * 100 * OFFICIAL_DIVISOR)
'''
PRICING = '''from .relay_models import OFFICIAL_VERSION, OFFICIAL_DIVISOR, DISCOUNT_VERSION
QWEN_MINIMUMS = {
    OFFICIAL_VERSION: 1_200_000, DISCOUNT_VERSION: 960_000}
RATE_DIVISORS = {
    DISCOUNT_VERSION: OFFICIAL_DIVISOR}
def metered_units(numerator, price_version):
    divisor = RATE_DIVISORS.get(price_version, 1)
    return (numerator + divisor - 1) // divisor
def with_minimum(units, price_version):
    return max(units, QWEN_MINIMUMS.get(price_version, 0)) if units > 0 else 0
'''


def runtime(sources):
    values = {}
    exec(sources['relay_models.py'], values)
    exec('\n'.join(sources['pricing.py'].splitlines()[1:]), values)
    return values


class RelayPricingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'qwen').mkdir()
        self.sources = {'relay_models.py': MODELS, 'pricing.py': PRICING}
        for name, source in self.sources.items():
            (self.root / 'qwen' / name).write_bytes(source.encode())
        self.baselines = {name: hashlib.sha256(source.encode()).hexdigest()
                          for name, source in self.sources.items()}

    def prepare(self):
        with patch.object(installer, 'BASELINES', self.baselines):
            return installer.prepare(self.root)

    def test_current_models_use_exact_versioned_rates_and_sonnet_baseline(self):
        old, new = runtime(self.sources), runtime(self.prepare())
        for model in ('claude-sonnet-5-5', 'gemini-3.8-flash', 'gpt-6-luna'):
            version, ir, ort = new['retail_rates'](model)
            self.assertEqual(version, installer.CURRENT_VERSION)
            before = old['retail_rates'](model)
            self.assertEqual((ir * 4, ort * 4), (before[1] * 5, before[2] * 5))
        self.assertEqual(new['retail_rates']('qwen3.8-max'), old['retail_rates']('qwen3.8-max'))
        sonnet = new['retail_yuan_rates']('claude-sonnet-5-5')
        gemini = new['retail_yuan_rates']('gemini-3.8-flash')
        self.assertEqual(tuple(rate / base for rate, base in zip(gemini, sonnet)), (0.375, 0.375))

    def test_pending_old_receipts_keep_divisor_and_minimum(self):
        old, new = runtime(self.sources), runtime(self.prepare())
        version, ir, ort = old['retail_rates']('claude-sonnet-5-5')
        for prompt, completion in ((1, 1), (10000, 1000), (0, 0)):
            numerator = prompt * ir + completion * ort
            before = old['with_minimum'](old['metered_units'](numerator, version), version)
            after = new['with_minimum'](new['metered_units'](numerator, version), version)
            self.assertEqual(after, before)
        self.assertEqual(new['QWEN_MINIMUMS'][installer.CURRENT_VERSION], 1_200_000)
        self.assertEqual(new['RATE_DIVISORS'][installer.CURRENT_VERSION], 10000)

    def test_baseline_conflict_never_writes_either_file(self):
        path = self.root / 'qwen/pricing.py'
        path.write_bytes((PRICING + '\n# different live revision\n').encode())
        before = {name: (self.root / 'qwen' / name).read_bytes() for name in self.sources}
        with patch.object(installer, 'BASELINES', self.baselines), self.assertRaises(ValueError):
            installer.apply(self.root)
        self.assertEqual(before, {name: (self.root / 'qwen' / name).read_bytes() for name in self.sources})

    def test_crlf_cloud_source_has_same_reviewed_baseline(self):
        expected = self.prepare()
        for name, source in self.sources.items():
            (self.root / 'qwen' / name).write_bytes(source.replace('\n', '\r\n').encode())
        with patch.object(installer, 'BASELINES', self.baselines):
            installer.apply(self.root)
        self.assertEqual(expected, {name: (self.root / 'qwen' / name).read_bytes().decode()
                                    for name in self.sources})


if __name__ == '__main__':
    unittest.main()
