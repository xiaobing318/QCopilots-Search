"""Finish deferred source materials for one immutable phase-1 package execution."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
sys.dont_write_bytecode = True
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, 'reconfigure'):
        stream.reconfigure(encoding='utf-8', errors='backslashreplace')


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def checked(path, item):
    if not path.is_file() or path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
        raise RuntimeError('Retained evidence changed: ' + str(path))


def phase1_helpers():
    """Import only the checked-in sibling, never a path supplied by a report."""
    path = Path(__file__).resolve().with_name('qcs-package-phase1.py')
    spec = importlib.util.spec_from_file_location('qcs_package_phase1', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_phase1_result(phase1):
    if (phase1.get('kind') != 'clean-candidate-binary-package-execution'
            or phase1.get('phase1Passed') is not True or phase1.get('passed') is not False
            or phase1.get('deliverablesComplete') is not False or phase1.get('sourceBundleStatus') != 'deferred'):
        raise RuntimeError('Expected a successful, explicitly incomplete binary package phase')


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase1-summary', type=Path, required=True)
    parser.add_argument('--phase1-summary-sha256', required=True)
    parser.add_argument('--reserve-gib', type=float, default=3.0)
    parser.add_argument('--peak-additional-gib', type=float, default=1.2)
    parser.add_argument('--execute', action='store_true')
    args = parser.parse_args(argv)
    args.phase1_summary = args.phase1_summary.expanduser().resolve()
    if not re.fullmatch('[0-9a-f]{64}', args.phase1_summary_sha256):
        raise ValueError('An immutable phase-1 summary SHA256 is required')
    phase1_helpers().validate_budget(args.reserve_gib, args.peak_additional_gib)
    return args


def main(argv=None):
    args = parse_args(argv)
    if not args.execute:
        print(json.dumps({'preparedOnly': True, 'phase1Summary': str(args.phase1_summary),
            'phase1SummarySha256': args.phase1_summary_sha256,
            'prerequisite': 'An immutable successful phase 1. This package closure does not attest capacity, CI or Computer Use acceptance',
            'steps': ['revalidate original clean source, build, full CTest, semantic and package evidence',
                      'create the single source bundle using --download', 'verify the actual source bundle',
                      'revalidate prior artifacts, write a new final deliverables closure'],
            'reserveGiB': args.reserve_gib, 'peakAdditionalGiB': args.peak_additional_gib}, indent=2))
        return 0
    if digest(args.phase1_summary) != args.phase1_summary_sha256:
        raise RuntimeError('Phase-1 summary differs from its explicitly supplied SHA256')
    phase1 = read(args.phase1_summary)
    validate_phase1_result(phase1)
    helpers = phase1_helpers()
    first_runner = Path(helpers.__file__).resolve()
    if (Path(phase1['runnerPath']).resolve() != first_runner
            or digest(first_runner) != phase1['runnerSha256']):
        raise RuntimeError('Phase-1 runner changed')
    inputs = phase1['inputs']
    source = Path(inputs['source'])
    external = Path(inputs['build_root'])
    build = external / inputs['config'].lower()
    run = args.phase1_summary.parent
    packages = run / 'pkg'
    evidence = helpers.load_tool(source, 'qcs-release-evidence.py', 'qcopilots_source_closure_evidence')
    manifest_path = evidence.safe_file(run, phase1['retainedArtifacts']['path'])
    checked(manifest_path, phase1['retainedArtifacts'])
    artifact_manifest = read(manifest_path)
    if artifact_manifest.get('kind') != 'phase1-retained-artifacts':
        raise RuntimeError('Unexpected retained-artifact manifest')
    if len({item['path'] for item in artifact_manifest['files']}) != len(artifact_manifest['files']):
        raise RuntimeError('Duplicate retained-artifact path')
    for item in artifact_manifest['files']:
        checked(evidence.safe_file(run, item['path']), item)
    source_before = read(run / 'source-before.json')
    receipt_path = Path(inputs['build_record'])
    receipt = read(receipt_path)
    identity = phase1['identity']
    old_checksums = packages / 'SHA256SUMS'
    checksums_before = digest(old_checksums)
    binaries = {name: build / 'bin' / (name + '.exe') for name in helpers.APPLICATIONS}
    runner_hash = digest(Path(__file__))

    def stable(allow_checksum_refresh=False):
        checks = {
            'phase1SummaryUnchanged': digest(args.phase1_summary) == args.phase1_summary_sha256,
            'phase1RunnerUnchanged': digest(first_runner) == phase1['runnerSha256'],
            'phase2RunnerUnchanged': digest(Path(__file__)) == runner_hash,
            'sourceUnchanged': evidence.source_identity(source) == identity and all(
                (source / name).is_file() and digest(source / name) == value for name, value in source_before.items()),
            'binariesUnchanged': all(digest(path) == phase1['binaryHashesBefore'][name] for name, path in binaries.items()),
            'buildRecordUnchanged': digest(receipt_path) == phase1['buildRecordSha256'],
            'semanticUnchanged': digest(Path(inputs['semantic_report'])) == phase1['semanticSha256'],
            'ctestEvidenceUnchanged': digest(Path(inputs['ctest_summary'])) == phase1['ctestSummarySha256']
                and digest(Path(inputs['ctest_execution_record'])) == phase1['ctestExecutionRecordSha256']
                and all(Path(item['path']).is_file() and Path(item['path']).stat().st_size == item['bytes']
                        and digest(Path(item['path'])) == item['sha256'] for item in phase1['originalCtestFiles']),
            'retainedManifestUnchanged': digest(manifest_path) == phase1['retainedArtifacts']['sha256'],
        }
        for item in artifact_manifest['files']:
            if allow_checksum_refresh and item['path'] == 'pkg/SHA256SUMS':
                continue
            checked(evidence.safe_file(run, item['path']), item)
        checks['retainedArtifactsUnchanged'] = True
        if not all(checks.values()):
            raise RuntimeError('Original evidence changed: ' + repr(checks))
        return checks

    stable()
    if evidence.valid_identity(identity) or identity != receipt['identity']:
        raise RuntimeError('Expected the same clean build identity')
    errors = evidence.verified_build(receipt_path.parent, receipt)
    candidate = Path(phase1['candidateRecord'])
    errors += evidence.inspect_candidate(candidate.parent, read(candidate))
    semantic = read(Path(inputs['semantic_report']))
    for distribution in phase1['packages'].values():
        report_path = Path(distribution['report'])
        errors += evidence.validate_package_release(read(report_path), report_path.parent,
            model_fingerprint=semantic['modelFingerprint'], extraction_fingerprint=semantic['extractionFingerprint'],
            platform_id='windows-x64', worker_sha256=phase1['binaryHashesBefore']['qcs-worker'])
    if errors:
        raise RuntimeError('Original package/build gate no longer passes: ' + repr(errors))
    if list(packages.glob('qcs-source-materials-*')):
        raise RuntimeError('This run already contains source materials. Do not overwrite previous execution evidence')
    reserve = int(args.reserve_gib * 1024 ** 3)
    admission = reserve + int(args.peak_additional_gib * 1024 ** 3)
    if min(shutil.disk_usage(path).free for path in (external, run)) < admission:
        raise RuntimeError('Insufficient space for deferred source materials plus the reserve')
    closure = Path(tempfile.mkdtemp(prefix='qcs-close-', dir=run))
    result = {'kind': 'clean-candidate-package-deliverables-closure', 'passed': False,
        'deliverablesComplete': False, 'identity': identity, 'phase1Summary': str(args.phase1_summary),
        'phase1SummarySha256': args.phase1_summary_sha256, 'phase2RunnerSha256': runner_hash,
        'buildRecord': str(receipt_path), 'buildRecordSha256': phase1['buildRecordSha256'],
        'sourceBundleStatus': 'running', 'startedAt': time.time(), 'steps': [],
        'millionSealValidation': 'Separate evidence is required. This closure does not attest capacity acceptance',
        'phase1ChecksumsSha256': checksums_before, 'reserveGiB': args.reserve_gib}
    environment = os.environ.copy()
    environment.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')

    def command(label, arguments, timeout):
        if min(shutil.disk_usage(path).free for path in (external, run)) < reserve:
            raise RuntimeError('Free-space reserve reached before ' + label)
        began = time.monotonic()
        with (closure / (label + '.stdout.log')).open('w', encoding='utf-8') as stdout, (closure / (label + '.stderr.log')).open('w', encoding='utf-8') as stderr:
            executed = subprocess.run(list(map(str, arguments)), cwd=external, env=environment,
                                      stdout=stdout, stderr=stderr, timeout=timeout)
        result['steps'].append({'name': label, 'command': list(map(str, arguments)),
                               'exitCode': executed.returncode, 'elapsedSeconds': time.monotonic() - began})
        write(closure / 'final-closure.json', result)
        if executed.returncode:
            raise RuntimeError(label + ' failed. Original logs: ' + str(closure))

    try:
        command('source-bundle', [sys.executable, '-B', source / 'tools/qcs-source-bundle.py',
            '--build-root', external, '--qt-root', inputs['qt_root'], '--output-dir', packages,
            '--download', '--reserve-gib', str(args.reserve_gib)], 3600)
        source_zip, = packages.glob('qcs-source-materials-*.zip')
        source_sha = digest(source_zip)
        command('source-integrity', [sys.executable, '-B', source / 'tools/qcs-source-bundle.py',
            '--verify-bundle', source_zip, '--expected-sha256', source_sha], 1800)
        verification = read(closure / 'source-integrity.stdout.log')
        if (verification.get('passed') is not True or verification.get('commit') != identity['checkoutCommit']
                or verification.get('workingTreeModified') is not False or verification.get('sha256') != source_sha):
            raise RuntimeError('Source materials do not bind the original clean candidate')
        with zipfile.ZipFile(source_zip) as archive:
            source_manifest = json.loads(archive.read('source-manifest.json'))
        if {item['path']: item['sha256'] for item in source_manifest['projectFiles']} != source_before:
            raise RuntimeError('Source archive differs from original clean source bytes')
        result['sourceBundle'] = {'path': str(source_zip), 'bytes': source_zip.stat().st_size, 'sha256': source_sha}
        result['sourceIntegrityReport'] = {'path': str(closure / 'source-integrity.stdout.log'),
            'sha256': digest(closure / 'source-integrity.stdout.log')}
        result['stability'] = stable(allow_checksum_refresh=True)
        result['assets'] = [{'path': str(path), 'bytes': path.stat().st_size, 'sha256': digest(path)}
            for path in sorted(packages.glob('qcs-*')) if path.is_file() and path.suffix != '.sha256']
        result['finalChecksumsSha256'] = digest(old_checksums)
        result['freeBytesAfterByPath'] = {str(path): shutil.disk_usage(path).free for path in (external, run)}
        if min(result['freeBytesAfterByPath'].values()) < reserve:
            raise RuntimeError('Final free space is below the required reserve')
        result.update(passed=True, deliverablesComplete=True, sourceBundleStatus='verified', pendingSteps=[])
    except Exception as error:
        result['error'] = str(error)
        result['sourceBundleStatus'] = 'failed'
    finally:
        result['finishedAt'] = time.time()
        write(closure / 'final-closure.json', result)
    print(json.dumps({'passed': result['passed'], 'deliverablesComplete': result['deliverablesComplete'],
        'closure': str(closure / 'final-closure.json'), 'error': result.get('error')}, ensure_ascii=False, indent=2))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
