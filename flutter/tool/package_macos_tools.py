"""Build the portable macOS flashing tools from pinned upstream sources.

Mirrors tool/package_linux_tools.py without a container. openFPGALoader and the
USB libraries it needs are compiled from pinned sources for DEPLOYMENT_TARGET and
re-linked relocatably; esptool is frozen with PyInstaller on a relocatable
CPython so the result runs on Macs without Homebrew or a system Python.
Requires cmake, make, pkg-config and the Xcode command line tools.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

DEPLOYMENT_TARGET = '12.0'
PYINSTALLER = 'pyinstaller==6.16.0'
SYSTEM_LIBRARY_PREFIXES = ('/usr/lib/', '/System/')

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--downloads')
args = parser.parse_args()
if platform.system() != 'Darwin' or platform.machine() != 'arm64':
    raise SystemExit('This flashing-tool package targets macOS on Apple silicon.')
tool_dir = Path(__file__).resolve().parent
shared_inputs = json.loads((tool_dir / 'linux/inputs.json').read_text())
inputs = {**json.loads((tool_dir / 'macos/inputs.json').read_text()),
          **{name: shared_inputs[name] for name in ('esptool_source', 'openFPGALoader')}}
output = Path(args.output).resolve()
downloads = Path(args.downloads).resolve() if args.downloads else output.parent / 'macos-tools-downloads'
key = hashlib.sha256(Path(__file__).read_bytes() + json.dumps(inputs, sort_keys=True).encode()
                     + DEPLOYMENT_TARGET.encode()).hexdigest()
stamp = output / 'packaging.json'
if stamp.exists():
    saved = json.loads(stamp.read_text())
    if saved.get('key') == key and all((output / f).is_file() and
            hashlib.sha256((output / f).read_bytes()).hexdigest() == sha
            for f, sha in saved['files'].items()):
        print('Verified cached macOS flashing tools.')
        raise SystemExit(0)

downloads.mkdir(parents=True, exist_ok=True)
for name, data in inputs.items():
    cached = downloads / (name + '.tar')
    if not cached.exists() or hashlib.sha256(cached.read_bytes()).hexdigest() != data['sha256']:
        incoming = cached.with_suffix('.incoming')
        urllib.request.urlretrieve(data['url'], incoming)
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != data['sha256']:
            incoming.unlink()
            raise SystemExit('Upstream tool download failed SHA-256 verification: ' + name)
        incoming.replace(cached)


def run(*command, cwd=None, env=None):
    subprocess.run([str(part) for part in command], check=True, cwd=cwd, env=env)


def extract(name, into):
    before = {entry.name for entry in into.iterdir()}
    with tarfile.open(downloads / (name + '.tar')) as tf:
        if hasattr(tarfile, 'data_filter'):
            tf.extractall(into, filter='data')
        else:
            tf.extractall(into)
    created = {entry.name for entry in into.iterdir()} - before
    if len(created) != 1:
        raise RuntimeError(f'{name} archive must contain exactly one top-level directory')
    return into / created.pop()


def dependencies(binary):
    lines = subprocess.check_output(['otool', '-L', str(binary)], text=True).splitlines()[1:]
    return [line.split()[0] for line in lines]


def minimum_os(binary):
    listing = subprocess.check_output(['otool', '-l', str(binary)], text=True)
    versions = re.findall(r'^\s+minos\s+(\d+(?:\.\d+)*)$', listing, re.M) + re.findall(
        r'LC_VERSION_MIN_MACOSX\n.*\n\s+version\s+(\d+(?:\.\d+)*)$', listing, re.M)
    if not versions:
        raise RuntimeError('No build version in ' + str(binary))
    return max(tuple(int(part) for part in version.split('.')) for version in versions)


output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='.macos-tools-', dir=output.parent) as tmp:
    work = Path(tmp)
    out = work / 'firmware'
    tools = out / 'tools'
    licenses = out / 'licenses'
    install = work / 'install'
    for folder in (tools / 'bin', tools / 'lib', licenses, install):
        folder.mkdir(parents=True)
    sources = {name: extract(name, work) for name in inputs}

    # Only the freshly built libraries are visible to pkg-config, so nothing
    # from Homebrew or MacPorts can leak into the bundle. The SDK ships zlib
    # without a .pc file, so one is written for the system library.
    sdk = subprocess.check_output(['xcrun', '--show-sdk-path'], text=True).strip()
    zlib_version = re.search(r'#define ZLIB_VERSION "([^"]+)"',
                             Path(sdk, 'usr/include/zlib.h').read_text()).group(1)
    (install / 'lib/pkgconfig').mkdir(parents=True)
    (install / 'lib/pkgconfig/zlib.pc').write_text(
        f'Name: zlib\nDescription: macOS SDK zlib\nVersion: {zlib_version}\nLibs: -lz\nCflags:\n')
    env = {**os.environ, 'MACOSX_DEPLOYMENT_TARGET': DEPLOYMENT_TARGET,
           'PKG_CONFIG_LIBDIR': str(install / 'lib/pkgconfig'),
           'CFLAGS': f'-mmacosx-version-min={DEPLOYMENT_TARGET}',
           'CXXFLAGS': f'-mmacosx-version-min={DEPLOYMENT_TARGET}',
           'LDFLAGS': f'-mmacosx-version-min={DEPLOYMENT_TARGET}'}
    jobs = str(os.cpu_count() or 2)
    cmake_common = ['-DCMAKE_BUILD_TYPE=Release', f'-DCMAKE_INSTALL_PREFIX={install}',
                    f'-DCMAKE_PREFIX_PATH={install}', f'-DCMAKE_OSX_DEPLOYMENT_TARGET={DEPLOYMENT_TARGET}',
                    '-DCMAKE_MACOSX_RPATH=ON', '-DCMAKE_INSTALL_NAME_DIR=@rpath',
                    '-DCMAKE_POLICY_VERSION_MINIMUM=3.5']

    def cmake_build(source, *options):
        build = work / ('build-' + source.name)
        run('cmake', '-S', source, '-B', build, *cmake_common, *options, env=env)
        run('cmake', '--build', build, '--parallel', jobs, env=env)
        run('cmake', '--install', build, env=env)

    run('./configure', f'--prefix={install}', '--disable-static', '--disable-udev',
        cwd=sources['libusb'], env=env)
    run('make', '-j', jobs, 'install', cwd=sources['libusb'], env=env)
    cmake_build(sources['hidapi'], '-DBUILD_SHARED_LIBS=ON', '-DHIDAPI_BUILD_HIDTEST=OFF',
                '-DHIDAPI_WITH_TESTS=OFF')
    cmake_build(sources['libftdi1'], '-DBUILD_SHARED_LIBS=ON', '-DSTATICLIBS=OFF', '-DFTDIPP=OFF',
                '-DPYTHON_BINDINGS=OFF', '-DEXAMPLES=OFF', '-DFTDI_EEPROM=OFF', '-DDOCUMENTATION=OFF',
                '-DBUILD_TESTS=OFF')
    cmake_build(sources['openFPGALoader'])

    program = tools / 'bin/openFPGALoader'
    shutil.copy2(install / 'bin/openFPGALoader', program)
    shutil.copytree(install / 'share/openFPGALoader', tools / 'share/openFPGALoader', symlinks=False)
    shutil.copy2(sources['openFPGALoader'] / 'LICENSE', licenses / 'openFPGALoader.txt')

    def notice_name(prefix, notice):
        name = f'{prefix}-{notice.name}'
        return name if name.endswith('.txt') else name + '.txt'

    library_sources = {'libusb-1.0': 'libusb', 'libftdi1': 'libftdi1', 'libhidapi': 'hidapi'}
    for name in library_sources.values():
        for notice in sorted(sources[name].glob('COPYING*')) + sorted(sources[name].glob('LICENSE*')):
            if notice.is_file():
                shutil.copy2(notice, licenses / notice_name(name, notice))

    # Copy every non-system library the program loads, directly or through
    # another library, into tools/lib and rewrite all load commands to @rpath so
    # the bundle has no dependency on where it was built.
    def locate(reference, loader):
        name = Path(reference).name
        if reference.startswith(('@rpath/', '@loader_path/', '@executable_path/')) or '/' not in reference:
            candidates = [install / 'lib' / name, loader.parent / name]
        else:
            candidates = [Path(reference)]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise RuntimeError(f'Cannot resolve {reference} needed by {loader}')

    records = []
    bundled = {}
    pending = [program]
    while pending:
        binary = pending.pop()
        for reference in dependencies(binary):
            if reference.startswith(SYSTEM_LIBRARY_PREFIXES):
                continue
            name = Path(reference).name
            if name not in bundled:
                origin = locate(reference, binary)
                if install.resolve() not in origin.parents:
                    raise RuntimeError(f'{origin} was not built from pinned sources')
                destination = tools / 'lib' / name
                shutil.copy2(origin, destination)
                destination.chmod(0o755)
                run('install_name_tool', '-id', '@rpath/' + name, destination)
                bundled[name] = destination
                pending.append(destination)
                package = next(value for key, value in library_sources.items() if name.startswith(key))
                records.append({'library': name, 'package': package, 'version': inputs[package]['version']})
            if reference != '@rpath/' + name:
                run('install_name_tool', '-change', reference, '@rpath/' + name, binary)
    run('install_name_tool', '-add_rpath', '@executable_path/../lib', program)
    for binary in bundled.values():
        run('install_name_tool', '-add_rpath', '@loader_path', binary)
    for binary in [program, *bundled.values()]:
        run('codesign', '--force', '--sign', '-', binary)
        for reference in dependencies(binary):
            if reference.startswith(SYSTEM_LIBRARY_PREFIXES):
                continue
            if not reference.startswith('@rpath/') or Path(reference).name not in bundled:
                raise RuntimeError(f'{binary} still loads {reference}')
    wrapper = tools / 'openFPGALoader'
    wrapper.write_text('''#!/bin/sh
set -eu
tool_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export OPENFPGALOADER_SOJ_DIR="$tool_dir/share/openFPGALoader"
exec "$tool_dir/bin/openFPGALoader" "$@"
''')
    wrapper.chmod(0o755)

    esp_src = sources['esptool_source']
    python = sources['cpython']
    venv = work / 'venv'
    run(python / 'bin/python3', '-m', 'venv', venv)
    run(venv / 'bin/pip', 'install', '--quiet', '--disable-pip-version-check', PYINSTALLER, esp_src)
    stubs = esp_src / 'esptool/targets/stub_flasher'
    run(venv / 'bin/pyinstaller', '--onefile', '--name', 'esptool', '--distpath', work / 'dist',
        *(part for folder in sorted(stubs.iterdir()) if folder.is_dir()
          for part in ('--add-data', f'{folder}:esptool/targets/stub_flasher/{folder.name}')),
        '--workpath', work / 'pyi-build', '--specpath', work / 'pyi-spec', '--log-level', 'WARN',
        esp_src / 'esptool.py', env=env)
    shutil.copy2(work / 'dist/esptool', tools / 'esptool')
    shutil.copy2(esp_src / 'LICENSE', licenses / 'esptool.txt')
    runtime_notices = licenses / 'esptool-runtime'
    runtime_notices.mkdir()
    embedded_notices = []
    python_license = next((python / 'lib').glob('python3.*/LICENSE.txt'))
    python_notice = runtime_notices / f'cpython-{inputs["cpython"]["version"]}-LICENSE.txt'
    shutil.copy2(python_license, python_notice)
    embedded_notices.append(str(python_notice.relative_to(licenses)))
    site = next((venv / 'lib').glob('python*/site-packages'))
    excluded = {'pip', 'pyinstaller', 'pyinstaller_hooks_contrib', 'setuptools', 'altgraph', 'macholib', 'packaging'}
    for info in sorted(site.glob('*.dist-info')):
        if info.name.split('-')[0].lower() in excluded:
            continue
        for notice in list(info.glob('LICENSE*')) + list(info.glob('licenses/*')):
            if notice.is_file():
                destination = runtime_notices / notice_name(info.name.removesuffix('.dist-info'), notice)
                shutil.copy2(notice, destination)
                embedded_notices.append(str(destination.relative_to(licenses)))

    target = tuple(int(part) for part in DEPLOYMENT_TARGET.split('.'))
    python_library = next((python / 'lib').glob('libpython3.*.dylib'))
    for binary in [program, *bundled.values(), tools / 'esptool', python_library]:
        if minimum_os(binary) > target:
            raise RuntimeError(f'{binary} requires a newer macOS than {DEPLOYMENT_TARGET}')

    manifest = {'schema_version': 1, 'portable_tools': True,
        'tools': {'esptool': ['tools/esptool'], 'openFPGALoader': ['tools/openFPGALoader']},
        'versions': {'esptool': {'version': re.search(r'esptool-v?([\d.]+)', esp_src.name).group(1),
                                 **inputs['esptool_source']},
                     'openFPGALoader': inputs['openFPGALoader'],
                     'cpython': inputs['cpython']},
        'deployment_target': DEPLOYMENT_TARGET,
        'libraries': records, 'embedded_esptool_notices': embedded_notices}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (licenses / 'README.txt').write_text(
        'esptool is frozen with PyInstaller from the unmodified Espressif source (GPL-2.0-or-later) on a\n'
        'relocatable CPython ' + inputs['cpython']['version'] + ' build from python-build-standalone (PSF).\n'
        'openFPGALoader is built without source changes from revision ' + inputs['openFPGALoader']['revision'] +
        ' (Apache-2.0) together with libusb (LGPL-2.1), libftdi1 (LGPL-2.1) and hidapi (BSD/GPL-3/HIDAPI)\n'
        'from the pinned sources listed in manifest.json. Mach-O load paths were adjusted for this\n'
        'relocatable bundle, which targets macOS ' + DEPLOYMENT_TARGET + ' and later on Apple silicon.\n')
    clean = {'PATH': '/usr/bin:/bin', 'HOME': os.environ.get('HOME', '/tmp')}
    run(tools / 'esptool', 'version', env=clean)
    run(wrapper, '-V', env=clean)
    files = {str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()
             for f in sorted(out.rglob('*')) if f.is_file()}
    (out / 'packaging.json').write_text(json.dumps({'key': key, 'files': files}, indent=2) + '\n')
    if output.exists():
        shutil.rmtree(output)
    out.rename(output)
print('Packaged macOS flashing tools:', output)
