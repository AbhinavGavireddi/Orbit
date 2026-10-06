"""Build and locally sign the Orbit app; no installation or OS permissions granted."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def compile_app(output=None):
    env = dict(os.environ)
    clt = Path("/Library/Developer/CommandLineTools")
    if clt.exists():
        env["DEVELOPER_DIR"] = str(clt)
        sdk = clt / "SDKs/MacOSX26.5.sdk"
        if sdk.exists():
            env["SDKROOT"] = str(sdk)
    env["CLANG_MODULE_CACHE_PATH"] = "/private/tmp/orbit-native-clang"
    args = ["swift", "build", "--build-system", "native", "--disable-sandbox",
            "--cache-path", "/private/tmp/orbit-native-spm", "--package-path", str(ROOT / "native"),
            "--scratch-path", str(ROOT / "native/.build-native"), "--configuration", "release"]
    subprocess.run([*args, "--product", "Orbit"], env=env, check=True)
    binary_dir = subprocess.check_output([*args, "--show-bin-path"], env=env, text=True).strip()
    app = Path(output) if output else ROOT / "dist/Orbit.app"
    executable = app / "Contents/MacOS/Orbit"
    executable.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(binary_dir) / "Orbit", executable)
    shutil.copy2(ROOT / "native/Info.plist", app / "Contents/Info.plist")
    return app


def main():
    app = compile_app()
    if str(Path(__file__).resolve().parent) not in sys.path:
        sys.path[:0] = [str(Path(__file__).resolve().parent)]
    from stable_sign import sign_app
    identity = sign_app(app)
    print("Built app signed as " + identity + ": " + str(app))


if __name__ == "__main__":
    main()
