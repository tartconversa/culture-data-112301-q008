"""检查合成资料的格式、指纹和直接外键，不作业务计算。"""
import hashlib
import json
from pathlib import Path


def check(root):
    root = Path(root)
    catalog = json.loads((root / 'data/catalog.json').read_text(encoding='utf-8'))
    loaded = {}
    for name, spec in catalog['tables'].items():
        path = root / 'data' / (name + '.jsonl')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != spec['sha256']:
            raise ValueError('资料指纹不符 ' + name)
        rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        if len(rows) != spec['row_count']:
            raise ValueError('资料行数不符 ' + name)
        identifiers = [r[spec['key']] for r in rows]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError('资料主键重复 ' + name)
        for row in rows:
            if set(row) != set(spec['fields']):
                raise ValueError('字段不符 ' + name)
            for field, definition in spec['fields'].items():
                value = row[field]
                if value is None and definition['nullable']:
                    continue
                if type(value).__name__ != definition['type']:
                    raise ValueError('字段类型不符 ' + name + '.' + field)
        loaded[name] = rows
    for name, spec in catalog['tables'].items():
        for field, (target, key) in spec['references'].items():
            keys = {r[key] for r in loaded[target]}
            for row in loaded[name]:
                if row[field] is not None and row[field] not in keys:
                    raise ValueError('直接引用不存在 ' + name + '.' + field)
    return {name: len(rows) for name, rows in loaded.items()}


if __name__ == '__main__':
    print(json.dumps(check(Path(__file__).resolve().parents[1]), ensure_ascii=False, sort_keys=True))
