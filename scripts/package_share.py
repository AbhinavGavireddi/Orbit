"""Build the disk image a friend opens without Docker or a checkout.

Run: python3 scripts/package_share.py
The image contains Orbit.app and a short open note. Keys are not packaged.
"""
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts")]
from build_mac import compile_app  # noqa: E402
from stable_sign import IDENTITY, identity_present, sign_app, sign_file  # noqa: E402

CACHE = Path.home() / "Library/Application Support/Orbit/build-cache"
NOTE = """Orbit

1. Drag Orbit into Applications.
2. The first time, Control-click Orbit and choose Open, then Open.
   macOS asks once because this copy is not from the App Store.
3. Paste your OpenAI key. The Jev key is optional. They stay in this Mac's keychain.
4. Allow Microphone, Speech Recognition, Accessibility, Screen Recording,
   and Input Monitoring. If a Settings pane opens, switch Orbit on.
5. Orbit sits in the Dock and in the menu bar as a sparkle. Wake it and speak,
   or type from Settings.

Quit Orbit from the menu before you replace it with a newer copy.
"""


def fetch_json(url):
    result = subprocess.run(
        ["curl", "-fsSL", "-H", "Accept: application/vnd.github+json", "-H", "User-Agent: orbit-package", url],
        check=True, capture_output=True)
    return json.loads(result.stdout)


def download(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and dest.stat().st_size > 0:
        return
    subprocess.run(["curl", "-fL", "--retry", "3", "-o", str(dest), url], check=True)


def python_home():
    if (CACHE / "python" / "bin" / "python3").exists():
        return CACHE / "python"
    arch = "aarch64" if platform.machine() == "arm64" else "x86_64"
    release = fetch_json("https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest")
    asset = next(item for item in release["assets"]
                 if "cpython-3.13" in item["name"] and f"{arch}-apple-darwin-install_only" in item["name"]
                 and item["name"].endswith(".tar.gz") and "debug" not in item["name"])
    archive = CACHE / asset["name"]
    download(asset["browser_download_url"], archive)
    with tarfile.open(archive) as bundle:
        bundle.extractall(CACHE, filter="data")
    binary = CACHE / "python" / "bin" / "python3"
    if not binary.exists():
        target = next((CACHE / "python" / "bin").glob("python3.*"))
        binary.symlink_to(target.name)
    return CACHE / "python"


def valkey_binary():
    binary = CACHE / "valkey-server"
    if binary.exists():
        return binary
    # The makefile splits an unquoted path on spaces, so the source cannot live
    # under "Application Support".
    source = Path("/tmp/orbit-valkey-build")
    release = fetch_json("https://api.github.com/repos/valkey-io/valkey/releases/latest")
    archive = CACHE / f"valkey-{release['tag_name']}.tar.gz"
    download(release["tarball_url"], archive)
    if source.exists():
        shutil.rmtree(source)
    source.mkdir()
    with tarfile.open(archive) as bundle:
        bundle.extractall(source, filter="data")
    root = next(path for path in source.iterdir() if path.is_dir())
    subprocess.run(["make", "-j", str(os.cpu_count() or 4), "MALLOC=libc", "BUILD_TLS=no"], cwd=root, check=True)
    shutil.copy2(root / "src" / "valkey-server", binary)
    binary.chmod(0o755)
    return binary


def site_packages(python):
    site = CACHE / "site"
    if (site / "fastapi").exists() and (site / "uvicorn").exists():
        return site
    if site.exists():
        shutil.rmtree(site)
    site.mkdir()
    # Prefer locked deps via uv when available; fall back to pip for offline share builds.
    uv = shutil.which("uv")
    if uv:
        subprocess.run([uv, "export", "--frozen", "--no-dev", "--no-emit-project", "-o", str(CACHE / "requirements.share.txt")],
                       cwd=ROOT, check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--target", str(site),
                        "-r", str(CACHE / "requirements.share.txt")], check=True)
    else:
        subprocess.run([str(python), "-m", "pip", "install", "--upgrade", "pip"], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--target", str(site),
                        "-r", str(ROOT / "requirements-dev.txt")], check=True)
    return site


def is_macho(path):
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic in {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe",
                     b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}


def copy_tree(source, dest):
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(source, dest, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".env"))


def assemble(app, py_home, site, redis):
    runtime = app / "Contents/Resources/orbit-runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    copy_tree(py_home, runtime / "python")
    copy_tree(site, runtime / "site")
    app_root = runtime / "app"
    copy_tree(ROOT / "shared", app_root / "shared")
    for service in ("task", "voice", "automation", "research", "decision"):
        copy_tree(ROOT / "services" / service, app_root / "services" / service)
    scripts = app_root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(ROOT / "scripts" / "host.py", scripts / "host.py")
    shutil.copy2(ROOT / "scripts" / "serve.py", scripts / "serve.py")
    (runtime / "bin").mkdir()
    shutil.copy2(redis, runtime / "bin" / "valkey-server")
    os.chmod(runtime / "bin" / "valkey-server", 0o755)


def sign_tree(app):
    # A trusted local identity keeps permission grants across later copies.
    # Until that trust prompt is allowed, this copy is signed ad-hoc. The
    # friend's grants then stick until the app file itself changes.
    identity = IDENTITY if identity_present() else "-"
    if identity == "-":
        print("Signing this copy ad-hoc. Allow the keychain trust prompt later to keep permissions across updates.")
        subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
        subprocess.run(["codesign", "--verify", "--strict", "--deep", str(app)], check=True)
        return
    files = [path for path in app.rglob("*") if path.is_file() and not path.is_symlink() and is_macho(path)]
    files.sort(key=lambda path: len(path.parts), reverse=True)
    for path in files:
        sign_file(path, identity)
    sign_app(app)


def main():
    app = compile_app()
    py_home = python_home()
    site = site_packages(py_home / "bin" / "python3")
    redis = valkey_binary()
    linked = subprocess.check_output(["otool", "-L", str(redis)], text=True)
    if "/opt/homebrew" in linked or "/usr/local/opt" in linked:
        raise SystemExit("The bundled server is linked to a machine-local library and cannot be shared.")
    assemble(app, py_home, site, redis)
    sign_tree(app)
    staging = ROOT / "dist" / "share"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / "Orbit.app", symlinks=True)
    (staging / "Read me first.txt").write_text(NOTE)
    image = ROOT / "dist" / "Orbit.dmg"
    subprocess.run(["hdiutil", "create", "-volname", "Orbit", "-srcfolder", str(staging),
                    "-ov", "-format", "UDZO", str(image)], check=True)
    print("Share this file: " + str(image))


if __name__ == "__main__":
    main()
