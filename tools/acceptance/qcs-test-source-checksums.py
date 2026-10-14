import argparse
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import weakref
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
spec = importlib.util.spec_from_file_location('qcopilots_search_sources', Path(__file__).resolve().parents[1] / 'qcs-source-bundle.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class ChecksumRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qcs-checksums-', dir=os.environ.get('QCOPILOTS_SEARCH_TEST_ROOT') or None)
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


class CandidateSourceRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qcs-source-candidate-',
            dir=os.environ.get('QCOPILOTS_SEARCH_TEST_ROOT') or None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'project'
        self.source.mkdir()
        self.lock = self.source / 'dependencies.lock.json'
        self.lock.write_bytes(b'{"schemaVersion":1}\n')
        (self.source / 'README.md').write_bytes(b'SYNTHETIC corresponding source fixture\n')
        source = patch.object(module, 'SOURCE', self.source)
        source.start()
        self.addCleanup(source.stop)
        self.git('init', '--quiet')
        self.git('add', '.')
        self.git('-c', 'user.name=QCS synthetic test', '-c', 'user.email=synthetic@example.invalid',
                 'commit', '--quiet', '-m', 'Synthetic source fixture')
        self.commit = self.git('rev-parse', 'HEAD')

    def git(self, *arguments):
        return module.subprocess.check_output(['git', '-c', 'core.autocrlf=false', '-C', str(self.source),
                                               *arguments], text=True).strip()

    def test_exact_clean_candidate_and_source_tree_are_recorded(self):
        identity = module.checkout_identity(self.commit)
        self.assertEqual(identity['commit'], self.commit)
        self.assertEqual(identity['sourceTree'], self.git('rev-parse', 'HEAD^{tree}'))
        self.assertFalse(identity['workingTreeModified'])

    def test_gpu_and_variant_identity_files_are_dependency_locks(self):
        names = ['dependencies.lock.json', 'config/build-variants.json', 'config/gpu-sdk.lock.json',
                 'config/gpu-license-sources.lock.json', 'cmake/qcs-native-deps.cmake', 'README.md']
        records = [{'path': name, 'sha256': '1' * 64, 'size': 42} for name in names]
        self.assertEqual([entry['path'] for entry in module.dependency_locks(records)], names[:-1])

    def test_wrong_candidate_or_dirty_checkout_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            module.checkout_identity('0' * 40)
        self.lock.write_bytes(b'changed source lock\n')
        with self.assertRaisesRegex(RuntimeError, 'clean checkout'):
            module.checkout_identity(self.commit)

    def test_source_change_after_snapshot_is_rejected(self):
        identity = module.checkout_identity()
        records = [{'path': relative, 'sha256': module.digest(path), 'size': path.stat().st_size}
                   for relative, path in module.project_files()]
        self.lock.write_bytes(b'changed source lock\n')
        with self.assertRaisesRegex(RuntimeError, 'identity changed'):
            module.verify_source_snapshot(identity, records)

    def test_candidate_bundle_is_platform_neutral_without_qt_sdk(self):
        build = self.root / 'external build'
        cache = build / 'deps/sources'
        cache.mkdir(parents=True)
        upstream = cache / 'synthetic.tar.gz'
        upstream.write_bytes(b'SYNTHETIC immutable source archive bytes')
        entry = {'name': 'synthetic', 'filename': upstream.name, 'sha256': module.digest(upstream),
                 'size': upstream.stat().st_size, 'url': 'https://example.invalid/source'}
        arguments = ['qcs-source-bundle.py', '--build-root', str(build), '--candidate-commit', self.commit,
                     '--reserve-gib', '0']
        with patch.object(sys, 'argv', arguments), patch.object(module, 'entries', return_value=[entry]), \
                patch.object(sys, 'stdout', new_callable=io.StringIO) as output:
            module.main()
        bundle = build / 'pkg' / f'qcs-source-materials-{self.commit[:12]}.zip'
        produced = json.loads(output.getvalue())
        self.assertTrue(produced['ok'])
        self.assertEqual(produced['bundle'], str(bundle))
        self.assertEqual(produced['sha256'], module.digest(bundle))
        result = module.verify_bundle(bundle, candidate_commit=self.commit)
        self.assertTrue(result['passed'])
        self.assertEqual(result['platform'], 'source')
        manifest = json.loads((build / 'pkg/qcs-source-manifest.json').read_text())
        self.assertEqual(manifest['architecture'], 'any')
        self.assertNotIn('qtSdkMetadata', manifest)
        self.assertEqual([entry['path'] for entry in manifest['dependencyLocks']], ['dependencies.lock.json'])
        with self.assertRaisesRegex(RuntimeError, 'does not match'):
            module.verify_bundle(bundle, candidate_commit='0' * 40)


class FFmpegMetadataRegression(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='qcs-ffmeta-',
            dir=os.environ.get('QCOPILOTS_SEARCH_TEST_ROOT') or None)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sdk = self.root / 'SDK with spaces \u6d4b\u8bd5'
        self.handle = Mock()
        dll_directories = patch.object(module.os, 'add_dll_directory', return_value=self.handle, create=True)
        dll_directories.start()
        self.addCleanup(dll_directories.stop)

    def kit(self, system='Linux', format_present=True):
        relative = {'Linux': 'gcc_64/lib/libavcodec.so.63',
                    'Darwin': 'macos/lib/libavcodec.63.dylib',
                    'Windows': 'msvc2022_64/bin/avcodec-63.dll'}[system]
        codec = self.sdk / '6.12.0' / relative
        codec.parent.mkdir(parents=True)
        codec.write_bytes(b'SYNTHETIC codec fixture, never loaded')
        if system == 'Linux' and format_present:
            (codec.parent / 'libavformat.so.63').write_bytes(b'SYNTHETIC format fixture, never loaded')
        return codec

    def codec(self, configuration=b'--disable-gpl --disable-nonfree'):
        library = Mock()
        library.avcodec_configuration.return_value = configuration
        library.avcodec_license.return_value = b'LGPL version 2.1 or later'
        library.avcodec_version.return_value = 63 << 16
        return library

    def test_linux_loads_local_format_group_first_and_retains_it_for_metadata(self):
        path = self.kit()
        library = self.codec()
        group = []
        calls = []
        class Group:
            pass
        def load(filename, **options):
            calls.append((filename, options))
            self.assertEqual(options, {'mode': module.ctypes.RTLD_LOCAL})
            if len(calls) == 1:
                self.assertEqual(Path(filename), path.parent / 'libavformat.so.63')
                dependency = Group()
                group.append(weakref.ref(dependency))
                return dependency
            self.assertEqual(Path(filename), path)
            self.assertIsNotNone(group[0](), 'Format dependency group must survive codec loading')
            return library
        def value(result):
            def read():
                self.assertIsNotNone(group[0](), 'Format dependency group must survive metadata reads')
                return result
            return read
        library.avcodec_configuration.side_effect = value(b'--disable-gpl')
        library.avcodec_license.side_effect = value(b'LGPL version 2.1 or later')
        library.avcodec_version.side_effect = value(63 << 16)
        with patch.object(module.platform, 'system', return_value='Linux'), patch.object(module.ctypes, 'CDLL', side_effect=load):
            result = module.ffmpeg_metadata(self.sdk)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result, {'binary': path.name, 'sha256': module.digest(path),
            'versionInteger': 63 << 16, 'configuration': '--disable-gpl', 'license': 'LGPL version 2.1 or later'})

    def test_linux_missing_same_kit_format_does_not_search_another_kit(self):
        self.kit(format_present=False)
        other = self.sdk / '6.12.0/unrelated/lib/libavformat.so.63'
        other.parent.mkdir(parents=True)
        other.write_bytes(b'SYNTHETIC unrelated kit')
        with patch.object(module.platform, 'system', return_value='Linux'), patch.object(module.ctypes, 'CDLL') as load:
            with self.assertRaisesRegex(RuntimeError, 'selected Qt FFmpeg directory lacks'):
                module.ffmpeg_metadata(self.sdk)
        load.assert_not_called()

    def test_linux_dependency_failure_prevents_codec_load_and_propagates(self):
        self.kit()
        failure = OSError('SYNTHETIC dependency load failure')
        with patch.object(module.platform, 'system', return_value='Linux'), patch.object(module.ctypes, 'CDLL', side_effect=failure) as load:
            with self.assertRaises(OSError) as caught:
                module.ffmpeg_metadata(self.sdk)
        self.assertIs(caught.exception, failure)
        self.assertEqual(load.call_count, 1)

    def test_non_linux_keeps_existing_codec_only_loading_and_metadata(self):
        for system in ('Windows', 'Darwin'):
            with self.subTest(system=system):
                path = self.kit(system)
                with patch.object(module.platform, 'system', return_value=system), patch.object(module.ctypes, 'CDLL', return_value=self.codec()) as load:
                    result = module.ffmpeg_metadata(self.sdk)
                load.assert_called_once_with(str(path))
                self.assertEqual(result['sha256'], module.digest(path))

    def test_linux_still_rejects_gpl_and_nonfree_configuration(self):
        self.kit()
        for flag in (b'--enable-gpl', b'--enable-nonfree'):
            with self.subTest(flag=flag), patch.object(module.platform, 'system', return_value='Linux'), patch.object(module.ctypes, 'CDLL', side_effect=[Mock(), self.codec(flag)]):
                with self.assertRaisesRegex(RuntimeError, 'incompatible GPL/nonfree'):
                    module.ffmpeg_metadata(self.sdk)

    def test_metadata_symbol_failure_propagates_and_closes_windows_directory(self):
        self.kit('Windows')
        library = self.codec()
        failure = OSError('SYNTHETIC metadata read failure')
        library.avcodec_configuration.side_effect = failure
        with patch.object(module.platform, 'system', return_value='Windows'), patch.object(module.ctypes, 'CDLL', return_value=library):
            with self.assertRaises(OSError) as caught:
                module.ffmpeg_metadata(self.sdk)
        self.assertIs(caught.exception, failure)
        if os.name == 'nt':
            self.handle.close.assert_called_once_with()

if __name__ == '__main__':
    unittest.main(verbosity=2)
