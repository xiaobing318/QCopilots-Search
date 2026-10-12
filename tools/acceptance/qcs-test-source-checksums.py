import argparse
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('qcopilots_search_sources', Path(__file__).resolve().parents[1] / 'qcs-source-bundle.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ChecksumRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qcs-checksums-', dir=os.environ.get('QCOPILOTS_SEARCH_TEST_ROOT'))
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / 'qcs-test.zip'
        self.package.write_bytes(b'installed package')
        self.bundle = self.root / 'qcs-source-materials-test.zip'
        self.bundle.write_bytes(b'source package')
        self.manifest = self.root / 'qcs-source-manifest.json'
        self.manifest.write_bytes(b'{"commit":"example"}')
        self.checksums = self.root / 'SHA256SUMS'
        self.original = module.digest(self.package) + '  ' + self.package.name + '\n'
        self.checksums.write_text(self.original, encoding='utf-8')

    def refresh(self):
        module.refresh_package_checksums(self.root, [self.bundle, self.manifest])

    def test_preserves_existing_and_adds_exact_completed_outputs(self):
        (self.root / 'qcs-unpublished.zip.part').write_bytes(b'incomplete')
        (self.root / (self.bundle.name + '.sha256')).write_bytes(b'sidecar')
        self.refresh()
        names = {line.split('  ', 1)[1] for line in self.checksums.read_text().splitlines()}
        self.assertEqual(names, {self.package.name, self.bundle.name, self.manifest.name})

    def test_first_source_bundle_creates_new_aggregate(self):
        self.checksums.unlink()
        self.refresh()
        self.assertEqual(len(self.checksums.read_text().splitlines()), 2)

    def test_rerun_updates_only_new_outputs(self):
        self.refresh()
        self.bundle.write_bytes(b'new source package')
        self.manifest.write_bytes(b'{"commit":"new"}')
        self.refresh()
        self.assertIn(module.digest(self.bundle), self.checksums.read_text())
        self.assertIn(self.original.strip(), self.checksums.read_text())

    def test_existing_modified_package_rejected_without_changing_manifest(self):
        self.package.write_bytes(b'changed')
        with self.assertRaisesRegex(RuntimeError, 'changed since checksum'):
            self.refresh()
        self.assertEqual(self.checksums.read_text(), self.original)

    def test_missing_existing_package_rejected(self):
        self.package.unlink()
        with self.assertRaisesRegex(RuntimeError, 'missing or linked'):
            self.refresh()

    def test_invalid_existing_names_and_duplicates_rejected(self):
        for name in ('../outside', 'qcs-/../outside', 'SHA256SUMS', 'qcs-file.sha256'):
            self.checksums.write_text('0' * 64 + '  ' + name + '\n')
            with self.assertRaisesRegex(RuntimeError, 'Invalid existing package checksum name'):
                self.refresh()
        self.checksums.write_text(self.original + self.original)
        with self.assertRaisesRegex(RuntimeError, 'Invalid existing package checksum name'):
            self.refresh()

    def test_failed_atomic_replace_preserves_old_manifest_and_cleans_own_temp(self):
        with patch.object(module.os, 'replace', side_effect=OSError('simulated replacement failure')):
            with self.assertRaisesRegex(OSError, 'simulated'):
                self.refresh()
        self.assertEqual(self.checksums.read_text(), self.original)
        self.assertFalse(list(self.root.glob('.qcs-checksums-*')))

    def test_checksum_file_cannot_include_itself(self):
        with self.assertRaisesRegex(RuntimeError, 'Invalid source checksum entry'):
            module.refresh_package_checksums(self.root, [self.checksums])

if __name__ == '__main__':
    unittest.main(verbosity=2)
