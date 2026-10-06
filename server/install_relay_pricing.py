"""Prepare versioned cloud model pricing; does not deploy or restart services."""
import argparse
import hashlib
from pathlib import Path

CURRENT_VERSION = 'olivia-official-20261006-fx67269'
BASELINES = {
    'relay_models.py': '1a0519783b10afc2e7ca5f0300b33502abf63b80473f94b27aacfe8b69251150',
    'pricing.py': 'f25644deb6f4ce94eee8376268b794f54e9e55b325dcbe9f9c8581fa729d0752',
}


def prepare(root):
    sources = {}
    for name, digest in BASELINES.items():
        source = (root / 'qwen' / name).read_bytes().decode('utf-8').replace('\r\n', '\n')
        if hashlib.sha256(source.encode('utf-8')).hexdigest() != digest:
            raise ValueError('Cloud pricing changed; review a fresh baseline')
        sources[name] = source

    def change(name, before, after):
        if sources[name].count(before) != 1:
            raise ValueError('Unexpected cloud pricing source')
        sources[name] = sources[name].replace(before, after, 1)

    change('relay_models.py', 'OFFICIAL_DIVISOR = 10000',
           f"CURRENT_VERSION = '{CURRENT_VERSION}'\nOFFICIAL_DIVISOR = 10000")
    change('relay_models.py',
           "    discount = Decimal('1') if row[1] == 'qwen' else Decimal('.8')\n"
           "    return Decimal(row[2]) * discount, Decimal(row[3]) * discount",
           '    return Decimal(row[2]), Decimal(row[3])')
    change('relay_models.py',
           "version = OFFICIAL_VERSION if NEW_MODELS[model][1] == 'qwen' else DISCOUNT_VERSION",
           "version = OFFICIAL_VERSION if NEW_MODELS[model][1] == 'qwen' else CURRENT_VERSION")
    change('pricing.py',
           'from .relay_models import OFFICIAL_VERSION, OFFICIAL_DIVISOR, DISCOUNT_VERSION',
           'from .relay_models import OFFICIAL_VERSION, OFFICIAL_DIVISOR, DISCOUNT_VERSION, CURRENT_VERSION')
    change('pricing.py',
           '    OFFICIAL_VERSION: 1_200_000, DISCOUNT_VERSION: 960_000}',
           '    OFFICIAL_VERSION: 1_200_000, DISCOUNT_VERSION: 960_000, CURRENT_VERSION: 1_200_000}')
    change('pricing.py',
           '    DISCOUNT_VERSION: OFFICIAL_DIVISOR}',
           '    DISCOUNT_VERSION: OFFICIAL_DIVISOR, CURRENT_VERSION: OFFICIAL_DIVISOR}')
    for name, source in sources.items():
        compile(source, name, 'exec')
    return sources


def apply(root):
    sources = prepare(root)
    for name, source in sources.items():
        (root / 'qwen' / name).write_bytes(source.encode('utf-8'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    apply(parser.parse_args().root)
