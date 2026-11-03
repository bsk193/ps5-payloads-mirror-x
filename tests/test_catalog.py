import json
import os
from pathlib import Path
import tempfile
import unittest
import catalog

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.previous = Path.cwd()
        self.temporary = tempfile.TemporaryDirectory()
        os.chdir(self.temporary.name)
    def tearDown(self):
        os.chdir(self.previous)
        self.temporary.cleanup()
    def test_shared_images_publish_separately_and_survive_catalog_updates(self):
        image = 'data:image/png;base64,aGVsbG8='
        first, second = {'name':'One'}, {'name':'Two'}
        catalog.assign_icon(first, image)
        self.assertTrue(Path('icons.json').exists())
        self.assertFalse(Path('payloads.json').exists())
        catalog.assign_icon(second, image)
        self.assertEqual(first['icon_key'], second['icon_key'])
        self.assertEqual(len(catalog.load_icons()), 1)
        first['version'] = '2.0'
        catalog.save_catalog([first, second])
        saved = json.loads(Path('payloads.json').read_text())
        self.assertIsInstance(saved, list)
        self.assertEqual(saved[0]['version'], '2.0')
        self.assertTrue(all('icon' not in entry for entry in saved))
        v2 = json.loads(Path('payloads_v2.json').read_text())
        self.assertEqual(v2['name'], 'PS5 Payloads')
        self.assertEqual(v2['icons_url'], 'icons.json')
        self.assertEqual(v2['payloads'][0]['version'], '2.0')
        self.assertTrue(all('icon' not in entry for entry in v2['payloads']))
        self.assertEqual(catalog.payload_icon(first), image)
        first.pop('icon_key'); catalog.save_catalog([first,second])
        self.assertIsNone(catalog.payload_icon(first))
        self.assertEqual(catalog.payload_icon(second), image)
        self.assertEqual(catalog.load_catalog(), [first, second])
    def test_invalid_images_do_not_change_files_or_entries(self):
        for image in ['https://bad.invalid/image.png', 'data:image/png;base64,', 'data:image/png;base64,!bad', 'data:image/png;base64,'+'A'*13000]:
            entry={'name':'One'}
            with self.assertRaises(ValueError):catalog.assign_icon(entry,image)
            self.assertEqual(entry, {'name':'One'})
            self.assertFalse(Path('icons.json').exists())
    def test_load_catalog_handles_wrapped_and_bare_formats(self):
        Path('payloads.json').write_text(json.dumps([{'name': 'One'}]))
        self.assertEqual(catalog.load_catalog(), [{'name': 'One'}])
        Path('payloads.json').unlink()
        Path('payloads_v2.json').write_text(json.dumps({'payloads': [{'name': 'Two'}]}))
        self.assertEqual(catalog.load_catalog(), [{'name': 'Two'}])

if __name__ == '__main__':unittest.main()
