"""Publication gates use synthetic fixtures in an isolated temporary directory."""
import copy
import importlib.util
import json
import re
from pathlib import Path
import shutil
import tempfile
import unittest

from PIL import Image

REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('catalog', REPO / 'scripts/catalog.py')
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='prompt-catalog-test-')
        self.root = Path(self.temp.name).resolve()
        # Check the isolated cleanup target before TemporaryDirectory removes it.
        self.assertTrue(self.root.is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.addCleanup(self.temp.cleanup)
        shutil.copytree(REPO / 'schemas', self.root / 'schemas')
        shutil.copytree(REPO / 'templates', self.root / 'templates')
        self.category = {'id': 'C01', 'name': '测试分类', 'slug': 'demo'}
        self.item = {'id': 'SC-001', 'category': 'C01', 'title': '测试场景', 'mode': 'G', 'style': '测试风格',
                     'mechanism': '测试结构', 'scene': '仅用于单元测试', 'input_count': 0, 'acceptance': '检查测试图', 'phase': 'pilot'}
        self.write('data/categories.json', [self.category])
        self.write('data/catalog.json', [self.item])
        self.base = self.root / 'prompts/demo/SC-001'

    def write(self, path, value):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')

    def make_entry(self, mode='G'):
        self.item.update(mode=mode, input_count=0 if mode == 'G' else 1)
        self.write('data/catalog.json', [self.item])
        (self.base / 'images').mkdir(parents=True)
        (self.base / 'prompt.md').write_text('A synthetic test image.\n', encoding='utf-8', newline='\n')
        output = self.base / 'images/result-01.png'
        Image.new('RGB', (4, 4), 'blue').save(output)
        inputs = []
        if mode != 'G':
            path = self.base / 'images/input-01.png'
            Image.new('RGB', (4, 4), 'red').save(path)
            inputs = [{'path': 'images/input-01.png', 'purpose': 'Synthetic fixture', 'sha256': catalog.digest(path)}]
        self.entry = {
            'schema_version': 1, 'id': 'SC-001', 'status': 'verified', 'prompt_file': 'prompt.md', 'inputs': inputs,
            'result': {'path': 'images/result-01.png', 'sha256': catalog.digest(output)},
            'generation': {'model': 'gpt-image-2.5-test-fixture', 'model_confirmed': True, 'interface': 'other',
                'generated_at': '2026-01-01T00:00:00Z', 'parameters': {}, 'prompt_sha256': catalog.digest(self.base / 'prompt.md')},
            'review': {'reviewer': 'unit-test-fixture', 'reviewed_at': '2026-01-01T01:00:00Z',
                'checks': {'prompt_alignment': 'pass', 'style_fidelity': 'pass', 'readability': 'pass', 'text_and_count': 'na',
                    'reference_fidelity': 'na' if mode == 'G' else 'pass', 'edit_scope': 'pass' if mode == 'E' else 'na'},
                'notes': 'Synthetic fixture: no text or count requirement; not a model test.'},
        }
        self.save_entry()

    def save_entry(self):
        self.write('prompts/demo/SC-001/entry.json', self.entry)

    def check_entry(self):
        return catalog.validate_entry(self.root, self.item, self.category, self.base)

    def test_planning_and_templates_are_not_published(self):
        self.assertEqual(catalog.build_or_check(self.root, 'build'), (1, 1, 0))
        self.assertEqual(catalog.read_json(self.root / 'data/gallery.json'), [])
        self.assertEqual(catalog.build_or_check(self.root, 'check'), (1, 1, 0))

    def test_complete_entry_appears_in_gallery_with_actual_result(self):
        self.make_entry()
        self.assertEqual(catalog.build_or_check(self.root, 'build'), (1, 1, 1))
        gallery = catalog.read_json(self.root / 'data/gallery.json')
        self.assertEqual(gallery[0]['preview'], 'prompts/demo/SC-001/images/result-01.png')

    def test_generated_preview_requires_review_and_keeps_actual_status(self):
        self.make_entry()
        self.entry.update(status='generated', review=None)
        self.entry['generation'].update(model=None, model_confirmed=False)
        self.save_entry()
        self.assertEqual(catalog.build_or_check(self.root, 'build'), (1, 1, 0))
        self.assertEqual(catalog.read_json(self.root / 'data/gallery.json'), [])
        self.entry['review'] = {'reviewer': 'unit-test-fixture', 'reviewed_at': '2026-01-01T01:00:00Z',
            'checks': {'prompt_alignment': 'pass', 'style_fidelity': 'pass', 'readability': 'pass',
                       'text_and_count': 'na', 'reference_fidelity': 'na', 'edit_scope': 'na'},
            'notes': 'Synthetic fixture without text or counts.'}
        self.save_entry()
        self.assertEqual(catalog.build_or_check(self.root, 'build'), (1, 1, 0))
        gallery = catalog.read_json(self.root / 'data/gallery.json')
        self.assertEqual(gallery[0]['status'], 'generated')
        self.assertIsNone(gallery[0]['model'])
        self.entry['review']['checks']['prompt_alignment'] = 'fail'
        self.save_entry()
        catalog.build_or_check(self.root, 'build')
        self.assertEqual(catalog.read_json(self.root / 'data/gallery.json'), [])

    def test_missing_or_corrupt_image_is_rejected(self):
        self.make_entry()
        path = self.base / 'images/result-01.png'
        original = path.read_bytes()
        path.unlink()
        with self.assertRaisesRegex(ValueError, 'Missing file'):
            self.check_entry()
        path.write_bytes(b'not an image')
        self.entry['result']['sha256'] = catalog.digest(path)
        self.save_entry()
        with self.assertRaises(OSError):
            self.check_entry()
        path.write_bytes(original)

    def test_prompt_and_input_tampering_are_rejected(self):
        self.make_entry('R')
        prompt = self.base / 'prompt.md'
        original = prompt.read_bytes()
        prompt.write_text('Changed after generation', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Prompt hash mismatch'):
            self.check_entry()
        prompt.write_bytes(original)
        Image.new('RGB', (4, 4), 'green').save(self.base / 'images/input-01.png')
        with self.assertRaisesRegex(ValueError, 'Image hash mismatch'):
            self.check_entry()

    def test_line_endings_cannot_change_prompt_hash_after_git_checkout(self):
        self.make_entry()
        prompt = self.base / 'prompt.md'
        prompt.write_bytes(b'Windows line endings\r\n')
        self.entry['generation']['prompt_sha256'] = catalog.digest(prompt)
        self.save_entry()
        with self.assertRaisesRegex(ValueError, 'LF line endings'):
            self.check_entry()

    def test_required_reference_and_edit_review_cannot_be_skipped(self):
        self.make_entry('E')
        original = copy.deepcopy(self.entry)
        for key in ['reference_fidelity', 'edit_scope']:
            with self.subTest(key=key):
                self.entry = copy.deepcopy(original)
                self.entry['review']['checks'][key] = 'na'
                self.save_entry()
                with self.assertRaises(ValueError):
                    self.check_entry()
        self.entry = original
        self.entry['inputs'] = []
        self.save_entry()
        with self.assertRaisesRegex(ValueError, 'Input count mismatch'):
            self.check_entry()

    def test_verified_requires_confirmed_target_model_and_completed_review(self):
        self.make_entry()
        original = copy.deepcopy(self.entry)
        mutations = [
            lambda e: e['generation'].update(model_confirmed=False),
            lambda e: e['generation'].update(model='different-image-model'),
            lambda e: e.update(review=None),
            lambda e: e['review']['checks'].update(text_and_count='fail'),
            lambda e: e['review'].update(reviewed_at='2025-12-31T00:00:00Z'),
        ]
        for i, mutate in enumerate(mutations):
            with self.subTest(mutation=i):
                self.entry = copy.deepcopy(original)
                mutate(self.entry)
                self.save_entry()
                with self.assertRaises(ValueError):
                    self.check_entry()

    def test_input_copy_cannot_be_published_as_result(self):
        self.make_entry('R')
        output = self.base / 'images/result-01.png'
        output.write_bytes((self.base / 'images/input-01.png').read_bytes())
        self.entry['result']['sha256'] = catalog.digest(output)
        self.save_entry()
        with self.assertRaisesRegex(ValueError, 'copy of an input'):
            self.check_entry()

    def test_duplicate_ids_and_wrong_category_paths_are_rejected(self):
        self.write('data/catalog.json', [self.item, self.item])
        with self.assertRaisesRegex(ValueError, 'Duplicate scene IDs'):
            catalog.load_catalog(self.root)
        self.write('data/catalog.json', [self.item])
        (self.root / 'prompts/unregistered').mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, 'Unknown category path'):
            catalog.load_catalog(self.root)

    def test_paths_cannot_escape_entry_directory(self):
        self.make_entry()
        with self.assertRaisesRegex(ValueError, 'outside entry directory'):
            catalog.asset_path(self.base, '../../../data/catalog.json')
        self.entry['result']['path'] = '../../../data/catalog.json'
        self.save_entry()
        with self.assertRaises(ValueError):
            self.check_entry()

    def test_sensitive_generation_parameters_are_rejected(self):
        self.make_entry()
        self.entry['generation']['parameters'] = {'nested': {'api_key': 'fake-unit-test-value'}}
        self.save_entry()
        with self.assertRaisesRegex(ValueError, 'Sensitive generation parameter'):
            self.check_entry()

    def test_check_detects_stale_pages_without_overwriting_them(self):
        catalog.build_or_check(self.root, 'build')
        readme = self.root / 'README.md'
        readme.write_text('Stale manual count: 999', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'Generated files are stale'):
            catalog.build_or_check(self.root, 'check')
        self.assertEqual(readme.read_text(encoding='utf-8'), 'Stale manual count: 999')

    def test_gallery_pagination_preserves_each_case_and_navigation(self):
        for count, expected_pages in [(0, 1), (20, 1), (21, 2), (100, 5)]:
            with self.subTest(count=count):
                records = [dict(id=f'SC-{n:03d}', title=f'Case {n}', page=f'prompts/case-{n}/README.md',
                                preview=f'prompts/case-{n}/images/result-01.png') for n in range(1, count + 1)]
                pages = catalog.render_gallery(records)
                self.assertEqual(len(pages), expected_pages)
                seen = []
                for path, content in pages.items():
                    ids = re.findall(r'^## \[(SC-\d+)', content, re.M)
                    self.assertLessEqual(len(ids), 20)
                    seen.extend(ids)
                    for destination in re.findall(r'\]\((gallery[^)]*\.md)\)', content):
                        self.assertIn(f'docs/{destination}', pages)
                self.assertEqual(seen, [record['id'] for record in records])

    def test_gallery_shrink_removes_only_surplus_generated_pages(self):
        catalog.build_or_check(self.root, 'build')
        obsolete = self.root / 'docs/gallery-2.md'
        unrelated = self.root / 'docs/gallery-notes.md'
        obsolete.write_text('Old gallery page', encoding='utf-8')
        unrelated.write_text('Manual notes', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'stale'):
            catalog.build_or_check(self.root, 'check')
        self.assertTrue(obsolete.exists())
        catalog.build_or_check(self.root, 'build')
        self.assertFalse(obsolete.exists())
        self.assertEqual(unrelated.read_text(encoding='utf-8'), 'Manual notes')


if __name__ == '__main__':
    unittest.main()
