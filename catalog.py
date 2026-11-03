"""Catalog and separately published, content-addressed icon data."""
import base64
import hashlib
import json
import os
import tempfile
from pathlib import Path

CATALOG = Path('payloads.json')
CATALOG_V2 = Path('payloads_v2.json')
ICONS = Path('icons.json')

def atomic_json(path, value):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(value, stream, indent=2, ensure_ascii=False)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

def load_catalog():
    target = CATALOG if CATALOG.exists() else CATALOG_V2
    if not target.exists():
        return []
    with open(target, 'r', encoding='utf-8') as f:
        data = json.load(f)
    if isinstance(data, dict):
        return data.get('payloads', [])
    return data

def save_catalog(payloads):
    atomic_json(CATALOG, payloads)
    atomic_json(CATALOG_V2, {
        'name': 'PS5 Payloads',
        'icons_url': 'icons.json',
        'payloads': payloads,
    })

def load_icons():
    if not ICONS.exists():
        return {}
    return json.loads(ICONS.read_text(encoding='utf-8'))['icons']

def payload_icon(payload):
    return load_icons().get(payload.get('icon_key'))

def assign_icon(payload, data):
    if not data.startswith(('data:image/png;base64,', 'data:image/jpeg;base64,', 'data:image/svg+xml;base64,')) or len(data) > 12288:
        raise ValueError('Expected a bounded base64 PNG, JPEG or SVG icon')
    if not base64.b64decode(data.split(',', 1)[1], validate=True):
        raise ValueError('Empty icon data')
    key = hashlib.sha256(data.encode('ascii')).hexdigest()
    icons = load_icons()
    icons[key] = data
    # Publish image data before publishing catalog references to it.
    atomic_json(ICONS, {'icons': icons})
    payload.pop('icon', None)
    payload['icon_key'] = key
