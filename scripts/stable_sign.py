"""Reuse one local code-signing identity so a rebuild keeps the same requirement.

The certificate and key stay in the login keychain. They are never written
into the repository. Ad-hoc signing is not a fallback: an ad-hoc requirement
contains the cdhash, so the next build looks like a different app and macOS
forgets Accessibility, Screen Recording, and Input Monitoring.
"""
from pathlib import Path
import subprocess
import tempfile

IDENTITY = "Orbit Local Codesign"
BUNDLE_ID = "dev.orbit.assistant"


def designated_requirement():
    return (f'designated => identifier "{BUNDLE_ID}" '
            f'and certificate leaf[subject.CN] = "{IDENTITY}"')


def identity_present():
    found = subprocess.run(["security", "find-identity", "-v", "-p", "codesigning"],
                           capture_output=True, text=True)
    return IDENTITY in found.stdout


def ensure_identity():
    """Create the identity once. A later call reuses the same certificate."""
    if identity_present():
        return IDENTITY
    config = """
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = Orbit Local Codesign
[ext]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = critical,codeSigning
"""
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        conf, key, cert, package = folder / "cert.cnf", folder / "key.pem", folder / "cert.pem", folder / "orbit.p12"
        conf.write_text(config)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", str(key), "-out", str(cert), "-days", "3650",
                        "-config", str(conf)], check=True, capture_output=True)
        subprocess.run(["openssl", "pkcs12", "-export", "-inkey", str(key), "-in", str(cert),
                        "-out", str(package), "-passout", "pass:orbit-local",
                        "-keypbe", "PBE-SHA1-3DES", "-certpbe", "PBE-SHA1-3DES", "-macalg", "SHA1"],
                       check=True, capture_output=True)
        keychain = Path.home() / "Library/Keychains/login.keychain-db"
        subprocess.run(["security", "import", str(package), "-k", str(keychain),
                        "-P", "orbit-local", "-A", "-f", "pkcs12",
                        "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"],
                       check=True, capture_output=True)
        if not identity_present():
            subprocess.run(["security", "add-trusted-cert", "-r", "trustAsRoot", "-p", "codeSign",
                            "-k", str(keychain), str(cert)], check=True, capture_output=True)
    if not identity_present():
        raise SystemExit("The local signing identity was imported but codesign cannot see it.")
    return IDENTITY


def sign_file(path, identity):
    subprocess.run(["codesign", "--force", "--timestamp=none", "--sign", identity, str(path)],
                   check=True, capture_output=True)


def sign_app(app):
    identity = ensure_identity()
    subprocess.run(["codesign", "--force", "--timestamp=none", "--sign", identity,
                    "--identifier", BUNDLE_ID, "-r", designated_requirement(), str(app)],
                   check=True)
    subprocess.run(["codesign", "--verify", "--strict", "--deep", str(app)], check=True)
    return identity
