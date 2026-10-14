import contextlib
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import warnings
import zipfile

import apply_update
import build_release


class UpdateBundleTests(unittest.TestCase):
    def test_unsigned_local_bundle_verifies_and_extracts(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            bundle = root / "gateway.btbundle"
            build_release.build(bundle)
            with mock.patch.object(apply_update, "STATE", root / "status.json"), \
                    mock.patch.object(apply_update, "PUBLIC_KEY", root / "missing-public.pem"):
                manifest, signed = apply_update.verify_and_extract(bundle, root / "extract")
            self.assertEqual(manifest["gateway_version"], "2.0.0")
            self.assertFalse(signed)
            self.assertTrue((root / "extract/tools/install_gateway.py").is_file())

    def test_duplicate_or_tampered_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            bundle = root / "gateway.btbundle"
            build_release.build(bundle)
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(bundle, "a") as archive:
                    archive.writestr("install.sh", b"tampered")
            with mock.patch.object(apply_update, "STATE", root / "status.json"), \
                    mock.patch.object(apply_update, "PUBLIC_KEY", root / "missing-public.pem"):
                with self.assertRaises(RuntimeError):
                    apply_update.verify_and_extract(bundle, root / "extract")

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL is required")
    def test_ed25519_signed_bundle_is_verified(self):
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory)
            private = root / "private.pem"
            public = root / "public.pem"
            bundle = root / "signed.btbundle"
            subprocess.run(["openssl", "genpkey", "-algorithm", "Ed25519",
                            "-out", private], check=True, capture_output=True)
            subprocess.run(["openssl", "pkey", "-in", private, "-pubout",
                            "-out", public], check=True, capture_output=True)
            build_release.build(bundle, private)
            with mock.patch.object(apply_update, "STATE", root / "status.json"), \
                    mock.patch.object(apply_update, "PUBLIC_KEY", public):
                _manifest, signed = apply_update.verify_and_extract(bundle, root / "extract")
            self.assertTrue(signed)


if __name__ == "__main__":
    unittest.main(verbosity=2)
