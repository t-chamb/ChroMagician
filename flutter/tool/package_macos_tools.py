"""Build the portable macOS flashing tools from the pinned upstream sources.

Mirrors tool/package_linux_tools.py without a container: openFPGALoader is
compiled from the pinned revision against Homebrew libraries, which are copied
beside it and re-linked relocatably; esptool is frozen with PyInstaller.
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
import sys
import tarfile
import tempfile
import urllib.request
import venv

PYINSTALLER = 'pyinstaller==6.16.0'

parser = argparse.ArgumentParser()
parser.add_argument('--output', required=True)
parser.add_argument('--downloads')
args = parser.parse_args()
if platform.system() != 'Darwin':
    raise SystemExit('This flashing-tool package targets macOS.')
recipe = Path(__file__).resolve().parent / 'linux'
inputs = json.loads((recipe / 'inputs.json').read_text())
inputs = {name: inputs[name] for name in ('esptool_source', 'openFPGALoader')}
output = Path(args.output).resolve()
downloads = Path(args.downloads).resolve() if args.downloads else output.parent / 'macos-tools-downloads'
key = hashlib.sha256(Path(__file__).read_bytes() + json.dumps(inputs, sort_keys=True).encode()
                     + platform.machine().encode()).hexdigest()
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
    cached = downloads / (name + '.tar.gz')
    if not cached.exists() or hashlib.sha256(cached.read_bytes()).hexdigest() != data['sha256']:
        incoming = cached.with_suffix('.incoming')
        urllib.request.urlretrieve(data['url'], incoming)
        if hashlib.sha256(incoming.read_bytes()).hexdigest() != data['sha256']:
            incoming.unlink()
            raise SystemExit('Upstream tool download failed SHA-256 verification: ' + name)
        incoming.replace(cached)


def run(*command, **kwargs):
    subprocess.run([str(part) for part in command], check=True, **kwargs)


def brew_prefix():
    return Path(subprocess.check_output(['brew', '--prefix'], text=True).strip())


def dylib_dependencies(binary):
    lines = subprocess.check_output(['otool', '-L', str(binary)], text=True).splitlines()[1:]
    return [line.split()[0] for line in lines]


def homebrew_formula(path, prefix):
    resolved = Path(path).resolve()
    cellar = (prefix / 'Cellar').resolve()
    if cellar not in resolved.parents:
        raise RuntimeError('Bundled library is not from Homebrew: ' + str(path))
    formula, version = resolved.relative_to(cellar).parts[:2]
    return formula, version, cellar / formula / version


output.parent.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix='.macos-tools-', dir=output.parent) as tmp:
    work = Path(tmp)
    out = work / 'firmware'
    tools = out / 'tools'
    licenses = out / 'licenses'
    for folder in (tools / 'bin', tools / 'lib', licenses):
        folder.mkdir(parents=True)
    for name in inputs:
        with tarfile.open(downloads / (name + '.tar.gz')) as tf:
            tf.extractall(work, filter='data')

    prefix = brew_prefix()
    src = work / ('openFPGALoader-' + inputs['openFPGALoader']['revision'])
    run('cmake', '-S', src, '-B', work / 'build', '-DCMAKE_BUILD_TYPE=Release',
        '-DCMAKE_INSTALL_PREFIX=' + str(work / 'install'),
        '-DCMAKE_PREFIX_PATH=' + str(prefix))
    run('cmake', '--build', work / 'build', '--parallel', str(os.cpu_count() or 2))
    run('cmake', '--install', work / 'build')
    program = tools / 'bin/openFPGALoader'
    shutil.copy2(work / 'install/bin/openFPGALoader', program)
    shutil.copytree(work / 'install/share/openFPGALoader', tools / 'share/openFPGALoader', symlinks=False)
    shutil.copy2(src / 'LICENSE', licenses / 'openFPGALoader.txt')

    # Copy Homebrew dylibs next to the program and point every load command at
    # them, so the bundle works on machines without Homebrew.
    records = []
    bundled = {}
    pending = [program]
    while pending:
        binary = pending.pop()
        for dependency in dylib_dependencies(binary):
            if dependency.startswith(('/usr/lib/', '/System/', '@')):
                continue
            name = Path(dependency).name
            if name not in bundled:
                formula, version, root = homebrew_formula(dependency, prefix)
                destination = tools / 'lib' / name
                shutil.copy2(Path(dependency).resolve(), destination)
                destination.chmod(0o755)
                run('install_name_tool', '-id', '@rpath/' + name, destination)
                bundled[name] = destination
                pending.append(destination)
                records.append({'library': name, 'package': formula, 'version': version})
                for notice in sorted(root.glob('COPYING*')) + sorted(root.glob('LICENSE*')):
                    shutil.copy2(notice, licenses / f'{formula}-{notice.name}.txt')
            run('install_name_tool', '-change', dependency, '@rpath/' + name, binary)
    run('install_name_tool', '-add_rpath', '@executable_path/../lib', program)
    for binary in [program, *bundled.values()]:
        run('install_name_tool', '-add_rpath', '@loader_path', binary)
        run('codesign', '--force', '--sign', '-', binary)
    wrapper = tools / 'openFPGALoader'
    wrapper.write_text('''#!/bin/sh
set -eu
tool_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export OPENFPGALOADER_SOJ_DIR="$tool_dir/share/openFPGALoader"
exec "$tool_dir/bin/openFPGALoader" "$@"
''')
    wrapper.chmod(0o755)

    esp_src = next(work.glob('esptool-*'))
    env = work / 'venv'
    venv.EnvBuilder(with_pip=True).create(env)
    pip = env / 'bin/pip'
    run(pip, 'install', '--quiet', '--disable-pip-version-check', PYINSTALLER, esp_src)
    stubs = esp_src / 'esptool/targets/stub_flasher'
    run(env / 'bin/pyinstaller', '--onefile', '--name', 'esptool', '--distpath', work / 'dist',
        *(part for folder in sorted(stubs.iterdir()) if folder.is_dir()
          for part in ('--add-data', f'{folder}:esptool/targets/stub_flasher/{folder.name}')),
        '--workpath', work / 'pyi-build', '--specpath', work / 'pyi-spec', '--log-level', 'WARN',
        esp_src / 'esptool.py')
    shutil.copy2(work / 'dist/esptool', tools / 'esptool')
    shutil.copy2(esp_src / 'LICENSE', licenses / 'esptool.txt')
    embedded_notices = []
    site = next((env / 'lib').glob('python*/site-packages'))
    for info in sorted(site.glob('*.dist-info')):
        package = info.name.split('-')[0].lower()
        if package in ('pip', 'pyinstaller', 'pyinstaller_hooks_contrib', 'setuptools', 'altgraph', 'macholib', 'packaging'):
            continue
        for notice in list(info.glob('LICENSE*')) + list(info.glob('licenses/*')):
            if notice.is_file():
                # Flat file names: codesign --deep treats dotted directory names
                # such as *.dist-info as malformed nested bundles.
                destination = licenses / 'esptool-runtime' / (
                    info.name.removesuffix('.dist-info') + '-' + notice.name + '.txt')
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(notice, destination)
                embedded_notices.append(str(destination.relative_to(licenses)))

    manifest = {'schema_version': 1, 'portable_tools': True,
        'tools': {'esptool': ['tools/esptool'], 'openFPGALoader': ['tools/openFPGALoader']},
        'versions': {'esptool': {'version': re.search(r'esptool-v?([\d.]+)', esp_src.name).group(1),
                                 **inputs['esptool_source']},
                     'openFPGALoader': inputs['openFPGALoader']},
        'libraries': records, 'embedded_esptool_notices': embedded_notices}
    (out / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    (licenses / 'README.txt').write_text(
        'esptool is frozen with PyInstaller from the unmodified Espressif source (GPL-2.0-or-later).\n'
        'openFPGALoader is built without source changes from revision ' + inputs['openFPGALoader']['revision'] +
        ' (Apache-2.0). Mach-O load paths were adjusted for this relocatable bundle.\n'
        'USB library notices and exact Homebrew formula versions accompany the bundle.\n')
    run(tools / 'esptool', 'version')
    run(wrapper, '-V')
    files = {str(f.relative_to(out)): hashlib.sha256(f.read_bytes()).hexdigest()
             for f in sorted(out.rglob('*')) if f.is_file()}
    (out / 'packaging.json').write_text(json.dumps({'key': key, 'files': files}, indent=2) + '\n')
    if output.exists():
        shutil.rmtree(output)
    out.rename(output)
print('Packaged macOS flashing tools:', output)
