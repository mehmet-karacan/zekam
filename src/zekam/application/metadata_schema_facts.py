import re

SAFE_ID = r'[A-Za-z][A-Za-z0-9_$#]{0,127}'
QUALIFIED = r'(?:' + SAFE_ID + r')\.(?:' + SAFE_ID + r')'


def _safe_ident(s):
    return bool(re.fullmatch(SAFE_ID, s))

def _qualified(s):
    return bool(re.fullmatch(QUALIFIED, s))

def _parse_bool(s):
    if s == 'true': return True
    if s == 'false': return False
    raise ValueError('bad bool')

def _parse_list(s):
    if s == '':
        raise ValueError('empty list')
    items = []
    for raw in s.split(','):
        x = raw.strip()
        if not x:
            raise ValueError('empty list item')
        if not _safe_ident(x):
            raise ValueError('bad list item')
        items.append(x)
    if len(set(items)) != len(items):
        raise ValueError('dup list item')
    return items

def parse_metadata_facts(records):
    tables = {}
    for rec in records:
        table = rec['table']
        if not _qualified(table): raise ValueError('bad table')
        text = rec['text']
        proof = rec['proof']
        if table not in tables:
            tables[table] = {'table': table, 'columns': {}, 'constraints': {}, 'proofs': []}
        t = tables[table]
        t['proofs'].append(proof)
        lines = [l for l in text.split('\n') if l.strip()]
        if not lines:
            continue
        first = lines[0]
        if first == 'Oracle metadata snapshot; structure only, not original DDL':
            lines = lines[1:]
        for line in lines:
            if line.startswith('TABLE '):
                parts = line.split()
                if len(parts) != 2 or not _qualified(parts[1]): raise ValueError('bad TABLE')
                if parts[1] != table: raise ValueError('TABLE mismatch')
                continue
            if line.startswith('INDEX '):
                parts = line.split(None, 2)
                if len(parts) < 2 or not _safe_ident(parts[1]): raise ValueError('bad INDEX')
                continue
            if line.startswith('COLUMN '):
                m = re.match(r'^COLUMN (' + SAFE_ID + r') type=(.*)$', line)
                if not m: raise ValueError('bad COLUMN')
                name, rest = m.group(1), m.group(2)
                nullable = None
                for tok in rest.split():
                    if tok.startswith('nullable='):
                        nullable = _parse_bool(tok[9:])
                        break
                if nullable is None: raise ValueError('missing nullable')
                type_str = ' '.join(tok for tok in rest.split() if not tok.startswith('nullable='))
                if name in t['columns']:
                    if t['columns'][name] != (type_str, nullable): raise ValueError('column conflict')
                else:
                    t['columns'][name] = (type_str, nullable)
                continue
            if line.startswith('CONSTRAINT '):
                m = re.match(r'^CONSTRAINT (' + SAFE_ID + r') kind=([PURC]) columns=\[([^\]]*)\]', line)
                if not m: raise ValueError('bad CONSTRAINT')
                name, kind, cols_str = m.groups()
                if kind == 'C' and cols_str == '':
                    cols = []
                else:
                    cols = _parse_list(cols_str)
                if kind in 'PUR' and not cols: raise ValueError('empty cols')
                rest = line[m.end():]
                target = None
                target_cols = []
                status = 'UNKNOWN'
                validated = 'UNKNOWN'
                if kind == 'R':
                    rm = re.search(r'REFERENCES (' + QUALIFIED + r'|' + SAFE_ID + r')(?: referenceColumns=\[([^\]]*)\])?', rest)
                    if rm:
                        raw = rm.group(1)
                        if not _qualified(raw):
                            if not _safe_ident(raw):
                                raise ValueError('bad FK target')
                            owner = table.split('.')[0]
                            target = owner + '.' + raw
                        else:
                            target = raw
                        if rm.group(2) is not None:
                            target_cols = _parse_list(rm.group(2))
                sm = re.search(r'(?:^|\s)status=([A-Z_]+)(?=\s|$)', rest)
                if sm: status = sm.group(1)
                vm = re.search(r'(?:^|\s)validated=(.*?)(?=\s+[A-Za-z]+=|$)', rest)
                if vm: validated = vm.group(1)
                if status not in {'UNKNOWN', 'ENABLED', 'DISABLED'} or validated not in {'UNKNOWN', 'VALIDATED', 'NOT VALIDATED', 'NOVALIDATE'}:
                    raise ValueError('unsupported constraint state')
                c = {'name': name, 'kind': kind, 'columns': cols, 'target': target, 'target_columns': target_cols, 'status': status, 'validated': validated, 'proof': proof}
                if name in t['constraints']:
                    old = t['constraints'][name]
                    for k in ['kind', 'columns', 'target', 'target_columns', 'status', 'validated']:
                        if old[k] != c[k]: raise ValueError('constraint conflict')
                else:
                    t['constraints'][name] = c
                continue
            raise ValueError('unknown line')
    result_tables = {}
    relations = []
    unresolved = 0
    for table in sorted(tables):
        t = tables[table]
        col_names = sorted(t['columns'].keys())
        for c in t['constraints'].values():
            for col in c['columns']:
                if col not in t['columns']: raise ValueError('missing col')
        pks = sorted((c for c in t['constraints'].values() if c['kind'] == 'P'), key=lambda c: c['name'])
        if len(pks) > 1: raise ValueError('multiple PK')
        pk = pks[0] if pks else None
        keys = sorted((c for c in t['constraints'].values() if c['kind'] in 'PU'), key=lambda c: c['name'])
        fks = sorted((c for c in t['constraints'].values() if c['kind'] == 'R'), key=lambda c: c['name'])
        result_tables[table] = {
            'table': table,
            'columns': col_names,
            'primary_key': pk,
            'keys': keys,
            'foreign_keys': fks,
            'proofs': t['proofs']
        }
        for fk in fks:
            tgt = fk['target']
            if tgt is None or tgt not in tables:
                unresolved += 1
                continue
            if not fk['target_columns'] or len(fk['columns']) != len(fk['target_columns']):
                unresolved += 1
                continue
            tt = tables[tgt]
            tkeys = [c for c in tt['constraints'].values() if c['kind'] in 'PU' and c['columns'] == fk['target_columns']]
            if not tkeys:
                unresolved += 1
                continue
            tk = sorted(tkeys, key=lambda c: c['name'])[0]
            if any(col not in tt['columns'] for col in fk['target_columns']):
                unresolved += 1
                continue
            relations.append({
                'source_table': table,
                'source_columns': fk['columns'],
                'target_table': tgt,
                'target_columns': fk['target_columns'],
                'constraint_name': fk['name'],
                'status': fk['status'],
                'validated': fk['validated'],
                'source_proof': fk['proof'],
                'target_proof': tk['proof'],
                'target_key': tk['name']
            })
    return {'tables': result_tables, 'relations': relations, 'unresolved_fk_count': unresolved}
