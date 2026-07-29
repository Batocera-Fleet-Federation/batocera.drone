import unittest
from unittest import mock

import app.common.install_paths as install_paths


class DroneInstallRootTests(unittest.TestCase):
    def test_flat_dev_checkout_layout(self) -> None:
        """No .releases segment: falls through to the original fixed-depth
        computation (<repo>/app/common/install_paths.py -> <repo>)."""
        fake_file = "/opt/dev-checkout/batocera.drone/app/common/install_paths.py"
        with mock.patch.object(install_paths, "__file__", fake_file):
            self.assertEqual(str(install_paths.drone_install_root()), "/opt/dev-checkout/batocera.drone")

    def test_release_versioned_layout_resolves_to_stable_root(self) -> None:
        """Regression test for a real live bug: <install root>/app is a
        symlink (app -> current/app -> .releases/<version>/app) under a
        release-versioned deploy layout. Python's import machinery reports
        __file__ already resolved *through* that symlink chain, so this must
        detect the .releases segment and walk up past it -- a fixed
        parents[2] would otherwise land inside the versioned release
        directory instead of the stable install root, which is exactly what
        broke VPN live (vpn_dir() is never user-configurable, so every
        connect() attempt pointed openvpn at a config path that doesn't
        exist there)."""
        fake_file = (
            "/userdata/system/drone-app/.releases/0.1.91-01da611bd234/app/common/install_paths.py"
        )
        with mock.patch.object(install_paths, "__file__", fake_file):
            self.assertEqual(str(install_paths.drone_install_root()), "/userdata/system/drone-app")

    def test_release_versioned_layout_regardless_of_version_string(self) -> None:
        fake_file = "/opt/drone/.releases/9.9.9-deadbeef1234/app/common/install_paths.py"
        with mock.patch.object(install_paths, "__file__", fake_file):
            self.assertEqual(str(install_paths.drone_install_root()), "/opt/drone")


if __name__ == "__main__":
    unittest.main()
