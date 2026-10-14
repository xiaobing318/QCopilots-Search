#!/usr/bin/env python3
"""Portable contract tests. They never package, download or modify a checkout."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace('-', '_'), HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PHASE1 = load('qcs-package-phase1')
PHASE2 = load('qcs-package-phase2')


class PackageRunnerContracts(unittest.TestCase):
    def setUp(self):
        self.context = tempfile.TemporaryDirectory(prefix='qcs-pkg-test-',
                                                  dir=os.environ.get('QCOPILOTS_SEARCH_TEST_ROOT') or None)
        self.addCleanup(self.context.cleanup)
        self.root = Path(self.context.name).resolve()
        self.source = self.root / '源码 checkout'
        self.external = self.root / '外置 build'
        self.output = self.root / '产物 output'
        self.source.mkdir()
        self.cwd = self.root / '任意 cwd'
        self.cwd.mkdir()

    def arguments(self):
        paths = {'source': self.source, 'build-root': self.external, 'qt-root': self.root / 'Qt SDK',
                 'qt-prefix': self.root / 'Qt SDK' / 'platform', 'build-record': self.root / 'build.json',
                 'semantic-report': self.root / 'semantic.json', 'ctest-summary': self.root / 'summary.json',
                 'ctest-execution-record': self.root / 'execution.json', 'output-root': self.output}
        args = [part for key, value in paths.items() for part in ('--' + key, str(value))]
        args += ['--head', 'a' * 40, '--tree', 'b' * 40, '--semantic-sha256', 'c' * 64,
                 '--build-record-sha256', 'd' * 64, '--defer-source-bundle']
        return args

    def run_script(self, filename, args):
        return subprocess.run([sys.executable, '-B', str(HERE / filename), *args], cwd=self.cwd,
                              text=True, encoding='utf-8', capture_output=True, timeout=30)

    def test_phase1_dry_run_from_unicode_arbitrary_cwd_does_not_create_outputs(self):
        completed = self.run_script('qcs-package-phase1.py', self.arguments())
        self.assertEqual(completed.returncode, 0, completed.stderr)
        report = json.loads(completed.stdout)
        self.assertTrue(report['preparedOnly'])
        self.assertEqual(Path(report['outputRoot']), self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.external.exists())
        self.assertFalse(list(self.source.iterdir()))
        self.assertNotIn('passed', report)

    def test_phase2_dry_run_never_reads_or_creates_missing_evidence(self):
        summary = self.output / 'phase1.json'
        completed = self.run_script('qcs-package-phase2.py',
                                    ['--phase1-summary', str(summary), '--phase1-summary-sha256', 'e' * 64])
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)['preparedOnly'])
        self.assertFalse(self.output.exists())

    def test_source_is_script_relative_and_model_default_is_selected_build_root(self):
        args = self.arguments()
        del args[:2]
        parsed = PHASE1.parse_args(args)
        self.assertEqual(parsed.source, HERE.parents[1])
        self.assertEqual(parsed.model_dir, self.external / 'models')
        self.assertEqual(PHASE1.APPLICATIONS, {'qcs-gui', 'qcs-cli', 'qcs-worker', 'qcs-service'})

    def test_tests_honor_nondefault_unicode_temporary_root_and_release_files(self):
        custom = self.root / '测试 temp root'
        custom.mkdir()
        environment = dict(os.environ, QCOPILOTS_SEARCH_TEST_ROOT=str(custom))
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve()),
                                 'PackageRunnerContracts.test_retained_evidence_checks_bytes_and_hash'],
                                cwd=self.cwd, env=environment, text=True, encoding='utf-8',
                                capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(custom.iterdir()), [])

    def test_identity_requires_full_immutable_hashes(self):
        for field, invalid in [('head', 'a' * 12), ('tree', 'A' * 40),
                               ('semantic-sha256', 'bad'), ('build-record-sha256', 'f' * 63)]:
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'immutable'):
                PHASE1.parse_args(self.arguments() + ['--' + field, invalid])

    def test_generated_paths_must_not_overlap_checkout_or_volume_root(self):
        for option in ('output-root', 'build-root'):
            for path in (self.source, self.source / 'nested', self.source.parent, Path(self.source.anchor)):
                with self.subTest(option=option, path=path), self.assertRaisesRegex(ValueError, 'outside'):
                    PHASE1.parse_args(self.arguments() + ['--' + option, str(path)])
        self.assertFalse(self.external.exists())

    def test_disk_budgets_reject_nonfinite_negative_and_insufficient_reserves(self):
        cases = [('reserve-gib', '2.9'), ('peak-additional-gib', '0.9'),
                 ('retained-additional-gib', '-1'), ('post-run-reserve-gib', '2.5')]
        cases += [(name, value) for name in ('reserve-gib', 'peak-additional-gib',
                                           'retained-additional-gib', 'post-run-reserve-gib')
                  for value in ('nan', 'inf', '-inf')]
        for name, value in cases:
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                PHASE1.parse_args(self.arguments() + ['--' + name + '=' + value])
        parsed = PHASE1.parse_args(self.arguments() + ['--reserve-gib', '5'])
        self.assertEqual(parsed.post_run_reserve_gib, 5)
        for value in ('2', 'nan', 'inf'):
            with self.subTest(phase2=value), self.assertRaises(ValueError):
                PHASE2.parse_args(['--phase1-summary', str(self.root / 'missing.json'),
                                  '--phase1-summary-sha256', 'f' * 64, '--reserve-gib', value])

    def test_configuration_cannot_escape_selected_build_root(self):
        for value in ('../other', 'Release/elsewhere', '', 'release'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'configuration'):
                PHASE1.parse_args(self.arguments() + ['--config', value])

    def ctest_inputs(self):
        identity = {'candidateHead': 'a' * 40, 'checkoutCommit': 'a' * 40, 'sourceTree': 'b' * 40}
        checks = {name: True for name in ('sourceUnchanged', 'binariesUnchanged',
                                         'runnerUnchanged', 'buildRecordUnchanged')}
        summary = {'kind': 'local-full-ctest-summary', 'passed': True, 'identity': identity,
                   'registeredPrograms': 2, 'passedPrograms': 2, 'sourceAndBinaryChecks': checks}
        execution = {'kind': 'full-ctest-execution', 'passed': True, 'identity': identity,
                     'endIdentity': identity, 'buildRecordSha256': 'd' * 64,
                     'semanticReportSha256': 'c' * 64, 'exitCode': 0, 'caseEvidenceExitCode': 0,
                     'semanticPassed': True, 'semanticValidationErrors': [], **checks}
        return summary, execution, identity

    def test_full_ctest_binding_accepts_only_matching_complete_candidate(self):
        summary, execution, identity = self.ctest_inputs()
        PHASE1.validate_ctest_bindings(summary, execution, identity, 'd' * 64, 'c' * 64)
        mutations = [('passed', False), ('passedPrograms', 1), ('registeredPrograms', 0),
                     ('registeredPrograms', -1), ('registeredPrograms', True),
                     ('identity', {'checkoutCommit': 'e' * 40}), ('sourceAndBinaryChecks', {})]
        for key, value in mutations:
            document = copy.deepcopy(summary)
            document[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RuntimeError):
                PHASE1.validate_ctest_bindings(document, execution, identity, 'd' * 64, 'c' * 64)

    def test_ctest_failure_or_replaced_build_semantic_or_runner_blocks_package(self):
        summary, execution, identity = self.ctest_inputs()
        mutations = [('passed', False), ('exitCode', 1), ('caseEvidenceExitCode', 1),
                     ('semanticPassed', False), ('semanticValidationErrors', ['missing fixture']),
                     ('buildRecordSha256', 'e' * 64), ('semanticReportSha256', 'f' * 64),
                     ('endIdentity', {}), ('sourceUnchanged', False), ('binariesUnchanged', False),
                     ('runnerUnchanged', False), ('buildRecordUnchanged', False)]
        for key, value in mutations:
            document = copy.deepcopy(execution)
            document[key] = value
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                PHASE1.validate_ctest_bindings(summary, document, identity, 'd' * 64, 'c' * 64)

    def test_phase1_pass_is_never_a_complete_deliverables_claim(self):
        valid = {'kind': 'clean-candidate-binary-package-execution', 'phase1Passed': True,
                 'passed': False, 'deliverablesComplete': False, 'sourceBundleStatus': 'deferred'}
        PHASE2.validate_phase1_result(valid)
        for key, value in [('phase1Passed', False), ('passed', True), ('deliverablesComplete', True),
                           ('sourceBundleStatus', 'verified'), ('kind', 'other')]:
            document = dict(valid, **{key: value})
            with self.subTest(key=key), self.assertRaises(RuntimeError):
                PHASE2.validate_phase1_result(document)

    def test_retained_evidence_checks_bytes_and_hash(self):
        path = self.root / '原始 report.json'
        path.write_bytes(b'original')
        item = {'bytes': 8, 'sha256': hashlib.sha256(b'original').hexdigest()}
        PHASE2.checked(path, item)
        path.write_bytes(b'replaced')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            PHASE2.checked(path, item)
        path.write_bytes(b'original plus more')
        with self.assertRaisesRegex(RuntimeError, 'changed'):
            PHASE2.checked(path, item)

    def test_phase2_loads_local_sibling_instead_of_report_selected_code(self):
        helpers = PHASE2.phase1_helpers()
        self.assertEqual(Path(helpers.__file__).resolve(), HERE / 'qcs-package-phase1.py')


if __name__ == '__main__':
    unittest.main(verbosity=2)
