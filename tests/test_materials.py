"""合成资料及资料检查入口的可复现检查。"""
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('material_check', ROOT / 'tools/check_materials.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class MaterialTests(unittest.TestCase):
    def test_format_and_references(self):
        counts = MODULE.check(ROOT)
        self.assertTrue(all(n > 0 for n in counts.values()))

    def test_corruption_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            shutil.copytree(ROOT / 'data', target / 'data')
            catalog = json.loads((target / 'data/catalog.json').read_text(encoding='utf-8'))
            name = next(iter(catalog['tables']))
            with (target / 'data' / (name + '.jsonl')).open('a', encoding='utf-8') as stream:
                stream.write('{}\n')
            with self.assertRaisesRegex(ValueError, '资料指纹不符'):
                MODULE.check(target)


if __name__ == '__main__':
    unittest.main()
