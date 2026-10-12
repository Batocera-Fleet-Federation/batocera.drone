import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.device import smtp_manager, usb_enumeration
from app.storage import audit_store
from tests.test_admin_fixes import build_settings

# Trimmed from the failed boot reported in issue #133 (adapter on root port 1-1).
FAILED_BOOT = """\
[    4.100000] usb 1-1: new full-speed USB device number 2 using xhci_hcd
[    4.400000] usb 1-1: device descriptor read/64, error -71
[    4.900000] usb usb1-port1: attempt power cycle
[    9.300000] usb 1-1: Device not responding to setup address.
[    9.500000] usb 1-1: device not accepting address 4, error -71
[    9.600000] usb usb1-port1: unable to enumerate USB device
[   12.000000] usb 5-1.4.1: new full-speed USB device number 7 using xhci_hcd
"""


def make_sysfs(root: Path, devices: dict, controller: str = "0000:c4:00.3") -> Path:
    """Fake /sys/bus/usb/devices with real symlinked root hubs under a PCI path."""
    sysfs = root / "sys" / "bus" / "usb" / "devices"
    sysfs.mkdir(parents=True)
    pci = root / "sys" / "devices" / "pci0000:c0" / controller
    for bus in (1, 5):
        hub = pci / f"usb{bus}"
        hub.mkdir(parents=True)
        (sysfs / f"usb{bus}").symlink_to(hub)
    for name, (vendor, product) in devices.items():
        node = sysfs / name
        node.mkdir()
        (node / "idVendor").write_text(vendor + "\n")
        (node / "idProduct").write_text(product + "\n")
    return sysfs


class ParseTests(unittest.TestCase):
    def test_root_port_failure_is_found_once_with_its_port(self) -> None:
        failures = usb_enumeration.find_root_port_failures(FAILED_BOOT + FAILED_BOOT)
        self.assertEqual([f["port"] for f in failures], ["1-1"])
        self.assertEqual((failures[0]["bus"], failures[0]["root_port"]), (1, 1))
        self.assertEqual(failures[0]["uptime"], 9.6)

    def test_healthy_boot_and_hub_port_noise_are_ignored(self) -> None:
        healthy = "[ 4.8] usb 5-1.4.1: new full-speed USB device number 7 using xhci_hcd\n"
        self.assertEqual(usb_enumeration.find_root_port_failures(healthy), [])
        hub_port = "[ 9.6] usb 1-1-port2: unable to enumerate USB device\n"
        self.assertEqual(usb_enumeration.find_root_port_failures(hub_port), [])
        self.assertEqual(usb_enumeration.find_root_port_failures(""), [])

    def test_adapter_presence_matches_vendor_and_product(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = make_sysfs(Path(tmp), {"5-1.4.1": ("057E", "0337"), "3-1": ("057e", "0306")})
            self.assertTrue(usb_enumeration.adapter_present(sysfs))
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = make_sysfs(Path(tmp), {"3-1": ("057e", "0306")})
            self.assertFalse(usb_enumeration.adapter_present(sysfs))
            self.assertFalse(usb_enumeration.adapter_present(Path(tmp) / "missing"))

    def test_controller_address_comes_from_the_root_hub_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sysfs = make_sysfs(Path(tmp), {})
            self.assertEqual(usb_enumeration._controller_address(1, sysfs), "0000:c4:00.3")
            self.assertEqual(usb_enumeration._controller_address(9, sysfs), "")


class CheckTests(unittest.TestCase):
    def _events(self, settings):
        return audit_store.list_unsent_events(settings, ["usb_enumeration_failed"], limit=10)

    def test_failure_without_adapter_notifies_once_per_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(usb_enumeration, "_boot_key", return_value="boot-a"):
            settings = build_settings(Path(tmp))
            sysfs = make_sysfs(Path(tmp), {})
            self.assertEqual(usb_enumeration.check_usb_enumeration(settings, FAILED_BOOT, sysfs), 1)
            self.assertEqual(usb_enumeration.check_usb_enumeration(settings, FAILED_BOOT, sysfs), 0)
            events = self._events(settings)
            self.assertEqual(len(events), 1)
            self.assertIn("1-1", events[0]["title"])
            self.assertIn("0000:c4:00.3", events[0]["title"])
            self.assertIn("different USB port", events[0]["message"])
            self.assertEqual(events[0]["details"]["port"], "1-1")
            self.assertEqual(events[0]["details"]["device_id"], settings.device_id)

    def test_new_boot_reports_the_same_port_again(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            sysfs = make_sysfs(Path(tmp), {})
            with mock.patch.object(usb_enumeration, "_boot_key", return_value="boot-a"):
                self.assertEqual(usb_enumeration.check_usb_enumeration(settings, FAILED_BOOT, sysfs), 1)
            with mock.patch.object(usb_enumeration, "_boot_key", return_value="boot-b"):
                self.assertEqual(usb_enumeration.check_usb_enumeration(settings, FAILED_BOOT, sysfs), 1)

    def test_adapter_working_elsewhere_stays_quiet(self) -> None:
        # The reported box: root port 1-1 logs -71, the adapter works on a hub port.
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            sysfs = make_sysfs(Path(tmp), {"5-1.4.1": ("057e", "0337")})
            self.assertEqual(usb_enumeration.check_usb_enumeration(settings, FAILED_BOOT, sysfs), 0)
            self.assertEqual(self._events(settings), [])

    def test_clean_kernel_log_stays_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            sysfs = make_sysfs(Path(tmp), {})
            self.assertEqual(usb_enumeration.check_usb_enumeration(settings, "[ 1.0] usb 1-1: ok\n", sysfs), 0)

    def test_check_is_read_only_and_throttled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = build_settings(Path(tmp))
            usb_enumeration._last_check_monotonic = None
            with mock.patch.object(usb_enumeration, "check_usb_enumeration", return_value=0) as check:
                usb_enumeration.check_usb_enumeration_throttled(settings)
                usb_enumeration.check_usb_enumeration_throttled(settings)
            self.assertEqual(check.call_count, 1)
            usb_enumeration._last_check_monotonic = None
        source = Path(usb_enumeration.__file__).read_text()
        for forbidden in ("unbind", "authorized", "write_text", "udevadm", "rescan"):
            self.assertNotIn(f"{forbidden}(", source)

    def test_unreadable_kernel_log_is_not_an_error(self) -> None:
        with mock.patch.object(usb_enumeration.subprocess, "run", side_effect=OSError("no dmesg")):
            self.assertEqual(usb_enumeration.read_kernel_log(), "")


class PollerIntegrationTests(unittest.TestCase):
    """UAT: a failed boot surfaces in the notification inbox via the Drone's poller tick."""

    def test_poller_tick_records_the_notification_and_it_is_emailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(usb_enumeration, "_boot_key", return_value="b"):
            settings = build_settings(Path(tmp))
            sysfs = make_sysfs(Path(tmp), {})
            usb_enumeration._last_check_monotonic = None

            class Stop(Exception):
                pass

            def one_tick(_seconds):
                if one_tick.done:
                    raise Stop()
                one_tick.done = True

            one_tick.done = False
            with mock.patch.object(usb_enumeration, "read_kernel_log", return_value=FAILED_BOOT), \
                 mock.patch.object(usb_enumeration, "USB_SYSFS_ROOT", sysfs), \
                 mock.patch.object(smtp_manager.time, "sleep", side_effect=one_tick), \
                 mock.patch.object(smtp_manager, "send_digest_if_needed"), \
                 mock.patch.object(smtp_manager, "process_outbound_mail_queue"), \
                 mock.patch.object(smtp_manager._crash_history, "ingest_new_crashes", return_value=0):
                with self.assertRaises(Stop):
                    smtp_manager.run_audit_email_digest_poller(settings)
            events = audit_store.list_unsent_events(settings, ["usb_enumeration_failed"], limit=10)
            self.assertEqual(len(events), 1)
            usb_enumeration._last_check_monotonic = None
            self.assertTrue(smtp_manager._load_state(settings)["notify"]["usb_enumeration_failed"])
            self.assertIn(
                "usb_enumeration_failed", smtp_manager._notifications.EVENT_TYPE_LABELS
            )


if __name__ == "__main__":
    unittest.main()
