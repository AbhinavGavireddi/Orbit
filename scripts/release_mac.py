"""Sign/notarize an assembled beta app with an explicitly configured Developer ID."""
import argparse
from pathlib import Path
import plistlib
import re
import shutil
import subprocess


def validate(identity, version, profile):
    if not identity.startswith('Developer ID Application:'):
        raise ValueError('A Developer ID Application identity is required; no ad-hoc fallback')
    if not re.fullmatch(r'\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?', version):
        raise ValueError('Version must be a safe semantic version')
    if not profile.strip():
        raise ValueError('Provide an existing notarytool Keychain profile name')


def run(*args):
    subprocess.run(args, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--app', type=Path, default=Path('dist/Orbit.app'))
    parser.add_argument('--identity', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--notary-profile', required=True)
    args = parser.parse_args()
    validate(args.identity, args.version, args.notary_profile)
    source = args.app.resolve()
    if not (source / 'Contents/Resources/orbit-runtime').is_dir():
        raise SystemExit('Assemble the self-contained runtime before releasing')
    staging = source.parent / ('release-' + args.version)
    if staging.exists():
        raise SystemExit('Version directory already exists; choose a new version or inspect it manually')
    staging.mkdir()
    app = staging / 'Orbit.app'
    shutil.copytree(source, app, symlinks=True)
    info = app / 'Contents/Info.plist'
    with info.open('rb') as stream:
        metadata = plistlib.load(stream)
    metadata['CFBundleShortVersionString'] = args.version.split('-')[0]
    metadata['CFBundleVersion'] = args.version.split('-')[0]
    with info.open('wb') as stream:
        plistlib.dump(metadata, stream)
    from package_share import is_macho
    files = sorted((p for p in app.rglob('*') if p.is_file() and not p.is_symlink() and is_macho(p)),
                   key=lambda p: len(p.parts), reverse=True)
    for path in files:
        run('codesign', '--force', '--options', 'runtime', '--timestamp', '--sign', args.identity, str(path))
    for bundle in sorted(app.rglob('*.framework'), key=lambda p: len(p.parts), reverse=True):
        run('codesign', '--force', '--options', 'runtime', '--timestamp', '--sign', args.identity, str(bundle))
    run('codesign', '--force', '--options', 'runtime', '--timestamp', '--sign', args.identity, str(app))
    run('codesign', '--verify', '--deep', '--strict', str(app))
    archive = staging / 'Orbit.zip'
    run('ditto', '-c', '-k', '--keepParent', str(app), str(archive))
    run('xcrun', 'notarytool', 'submit', str(archive), '--keychain-profile', args.notary_profile, '--wait')
    run('xcrun', 'stapler', 'staple', str(app))
    run('spctl', '--assess', '--type', 'execute', '--verbose', str(app))
    image = source.parent / ('Orbit-' + args.version + '.dmg')
    run('hdiutil', 'create', '-volname', 'Orbit', '-srcfolder', str(app), '-format', 'UDZO', str(image))
    run('codesign', '--timestamp', '--sign', args.identity, str(image))
    run('xcrun', 'notarytool', 'submit', str(image), '--keychain-profile', args.notary_profile, '--wait')
    run('xcrun', 'stapler', 'staple', str(image))
    print('Release artifact:', image)


if __name__ == '__main__':
    main()
