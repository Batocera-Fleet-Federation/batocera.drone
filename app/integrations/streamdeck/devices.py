"""Stream Deck hardware abstraction.

The runtime, API and UI only ever see ``StreamDeckDevice`` / its
``get_capabilities()`` dict -- never python-elgato-streamdeck objects. That
keeps hardware mocking trivial (``FakeStreamDeckDevice``), supports every model
the library supports (layout and key-image geometry are queried, never
assumed), and would let the library be replaced without touching callers.

``detect_usb_devices`` is a stdlib sysfs scan used before tooling is installed
(and as a cheap hotplug signal); its model table is a display hint only.
"""

import hashlib
import threading
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import safe_id


ELGATO_VENDOR_ID = 0x0FD9
# Display hints for pre-install detection only; real geometry always comes from
# the library once the worker opens the device.
USB_MODEL_HINTS: Dict[int, Tuple[str, int, int]] = {
    0x0060: ("Stream Deck", 3, 5),
    0x006D: ("Stream Deck (v2)", 3, 5),
    0x0080: ("Stream Deck MK.2", 3, 5),
    0x0063: ("Stream Deck Mini", 2, 3),
    0x0090: ("Stream Deck Mini MK.2", 2, 3),
    0x006C: ("Stream Deck XL", 4, 8),
    0x008F: ("Stream Deck XL (v2)", 4, 8),
    0x009A: ("Stream Deck Neo", 2, 4),
    0x0084: ("Stream Deck +", 2, 4),
    0x0086: ("Stream Deck Pedal", 1, 3),
}

KeyCallback = Callable[["StreamDeckDevice", int, bool], None]


def stable_device_id(serial: Any, model: Any, fallback: Any = "") -> str:
    """Serial when it is a safe identifier, otherwise a stable hash."""
    serial_text = str(serial or "").strip()
    try:
        return safe_id(serial_text, "serial")
    except ValueError:
        identity = f"{serial_text}|{model}|{fallback}"
        return "deck-" + hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:20]


class StreamDeckDevice(ABC):
    """One physical deck. Implementations must be safe to call from any thread."""

    @property
    @abstractmethod
    def transport_id(self) -> str:
        """Identifier stable while plugged in (the HID path) -- used for hotplug."""

    @property
    @abstractmethod
    def model(self) -> str: ...

    @property
    @abstractmethod
    def key_count(self) -> int: ...

    @property
    @abstractmethod
    def rows(self) -> int: ...

    @property
    @abstractmethod
    def columns(self) -> int: ...

    @property
    @abstractmethod
    def key_image_size(self) -> Tuple[int, int]:
        """(width, height) of one key image; (0, 0) for decks without key displays."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def reset(self) -> None: ...

    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def serial(self) -> str: ...

    @abstractmethod
    def firmware(self) -> str: ...

    @abstractmethod
    def set_brightness(self, percent: int) -> None: ...

    @abstractmethod
    def set_key_image(self, key: int, native_image: Any) -> None: ...

    @abstractmethod
    def to_native(self, image: Any) -> Any:
        """Convert a rendered key image to this model's native format/rotation."""

    @abstractmethod
    def register_key_callback(self, callback: Optional[KeyCallback]) -> None: ...

    @property
    def has_key_images(self) -> bool:
        width, height = self.key_image_size
        return width > 0 and height > 0

    def clear_key(self, key: int) -> None:
        self.set_key_image(key, None)

    def device_id(self) -> str:
        return stable_device_id(self.serial(), self.model, self.transport_id)

    def get_capabilities(self) -> dict:
        width, height = self.key_image_size
        return {
            "id": self.device_id(),
            "model": self.model,
            "serial": self.serial(),
            "firmware": self.firmware(),
            "key_count": int(self.key_count),
            "rows": int(self.rows),
            "columns": int(self.columns),
            "key_image_size": [int(width), int(height)],
            "has_key_images": self.has_key_images,
            "connected": self.connected(),
        }


class DeviceProvider(ABC):
    @abstractmethod
    def enumerate(self) -> List[StreamDeckDevice]:
        """Currently attached decks (unopened)."""


class LibraryStreamDeckDevice(StreamDeckDevice):
    """Adapter over a python-elgato-streamdeck deck (imported only in the worker)."""

    def __init__(self, deck: Any, pil_helper: Any) -> None:
        self._deck = deck
        self._pil_helper = pil_helper
        self._lock = threading.RLock()
        self._serial = ""
        self._firmware = ""
        try:
            self._transport_id = str(deck.id())
        except Exception:  # noqa: BLE001 - library/transport specific
            self._transport_id = str(id(deck))

    @property
    def transport_id(self) -> str:
        return self._transport_id

    @property
    def model(self) -> str:
        return str(self._deck.deck_type())

    @property
    def key_count(self) -> int:
        return int(self._deck.key_count())

    @property
    def rows(self) -> int:
        return int(self._deck.key_layout()[0])

    @property
    def columns(self) -> int:
        return int(self._deck.key_layout()[1])

    @property
    def key_image_size(self) -> Tuple[int, int]:
        is_visual = getattr(self._deck, "is_visual", None)
        if callable(is_visual) and not is_visual():
            return (0, 0)
        image_format = self._deck.key_image_format() or {}
        size = image_format.get("size") or (0, 0)
        return (int(size[0]), int(size[1]))

    def open(self) -> None:
        with self._lock:
            self._deck.open()
            for attribute, getter in (("_serial", "get_serial_number"), ("_firmware", "get_firmware_version")):
                try:
                    setattr(self, attribute, str(getattr(self._deck, getter)() or "").strip())
                except Exception:  # noqa: BLE001 - optional on some models
                    pass

    def close(self) -> None:
        with self._lock:
            try:
                self._deck.set_key_callback(None)
            except Exception:  # noqa: BLE001
                pass
            self._deck.close()

    def reset(self) -> None:
        with self._lock:
            self._deck.reset()

    def connected(self) -> bool:
        try:
            return bool(self._deck.connected())
        except Exception:  # noqa: BLE001
            return False

    def serial(self) -> str:
        return self._serial

    def firmware(self) -> str:
        return self._firmware

    def set_brightness(self, percent: int) -> None:
        with self._lock:
            self._deck.set_brightness(min(100, max(0, int(percent))))

    def set_key_image(self, key: int, native_image: Any) -> None:
        with self._lock:
            self._deck.set_key_image(int(key), native_image)

    def to_native(self, image: Any) -> Any:
        convert = getattr(self._pil_helper, "to_native_key_format", None) or self._pil_helper.to_native_format
        return convert(self._deck, image)

    def register_key_callback(self, callback: Optional[KeyCallback]) -> None:
        if callback is None:
            self._deck.set_key_callback(None)
            return
        self._deck.set_key_callback(lambda _deck, key, state: callback(self, int(key), bool(state)))


class LibraryDeviceProvider(DeviceProvider):
    """Enumerate through ``StreamDeck.DeviceManager`` (worker process only)."""

    def __init__(self) -> None:
        from StreamDeck.DeviceManager import DeviceManager  # noqa: WPS433 - private lib/ only
        from StreamDeck.ImageHelpers import PILHelper

        self._manager = DeviceManager()
        self._pil_helper = PILHelper

    def enumerate(self) -> List[StreamDeckDevice]:
        return [LibraryStreamDeckDevice(deck, self._pil_helper) for deck in self._manager.enumerate()]


class FakeStreamDeckDevice(StreamDeckDevice):
    """In-memory deck for tests and development: images, brightness, presses, hotplug."""

    def __init__(self, *, model: str = "Stream Deck Mini", serial: str = "FAKE0001", rows: int = 2,
                 columns: int = 3, key_image_size: Tuple[int, int] = (80, 80), firmware: str = "1.0.0",
                 transport_id: Optional[str] = None) -> None:
        self._model = model
        self._serial_number = serial
        self._rows = rows
        self._columns = columns
        self._size = key_image_size
        self._firmware_version = firmware
        self._transport = transport_id or f"fake:{serial}"
        self.is_connected = True
        self.is_open = False
        self.brightness: Optional[int] = None
        self.images: Dict[int, Any] = {}
        self.image_history: List[Tuple[int, Any]] = []
        self.reset_count = 0
        self.open_count = 0
        self.fail_writes = False
        self._callback: Optional[KeyCallback] = None
        self._lock = threading.RLock()

    @property
    def transport_id(self) -> str:
        return self._transport

    @property
    def model(self) -> str:
        return self._model

    @property
    def key_count(self) -> int:
        return self._rows * self._columns

    @property
    def rows(self) -> int:
        return self._rows

    @property
    def columns(self) -> int:
        return self._columns

    @property
    def key_image_size(self) -> Tuple[int, int]:
        return self._size

    def _require(self) -> None:
        if not self.is_connected:
            raise OSError("device disconnected")
        if not self.is_open:
            raise OSError("device is not open")

    def open(self) -> None:
        if not self.is_connected:
            raise OSError("device disconnected")
        self.is_open = True
        self.open_count += 1

    def close(self) -> None:
        self.is_open = False
        self._callback = None

    def reset(self) -> None:
        self._require()
        self.images = {}
        self.reset_count += 1

    def connected(self) -> bool:
        return self.is_connected

    def serial(self) -> str:
        return self._serial_number

    def firmware(self) -> str:
        return self._firmware_version

    def set_brightness(self, percent: int) -> None:
        self._require()
        self.brightness = min(100, max(0, int(percent)))

    def set_key_image(self, key: int, native_image: Any) -> None:
        with self._lock:
            self._require()
            if self.fail_writes:
                raise OSError("simulated write failure")
            if not 0 <= int(key) < self.key_count:
                raise IndexError("invalid key")
            self.images[int(key)] = native_image
            self.image_history.append((int(key), native_image))

    def to_native(self, image: Any) -> Any:
        return ("native", self._model, image)

    def register_key_callback(self, callback: Optional[KeyCallback]) -> None:
        self._callback = callback

    # -- simulation helpers ---------------------------------------------
    def key_down(self, key: int) -> None:
        if self._callback:
            self._callback(self, key, True)

    def key_up(self, key: int) -> None:
        if self._callback:
            self._callback(self, key, False)

    def press(self, key: int) -> None:
        self.key_down(key)
        self.key_up(key)

    def unplug(self) -> None:
        self.is_connected = False
        self.is_open = False

    def plug_in(self) -> None:
        self.is_connected = True


class FakeDeviceProvider(DeviceProvider):
    def __init__(self, devices: Optional[List[FakeStreamDeckDevice]] = None) -> None:
        self.devices = list(devices or [])
        self.enumerations = 0

    def enumerate(self) -> List[StreamDeckDevice]:
        self.enumerations += 1
        return [device for device in self.devices if device.is_connected]


def _read_sysfs(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def detect_usb_devices(sys_root: Path = Path("/sys/bus/usb/devices")) -> List[dict]:
    """Elgato USB devices visible to the kernel (Linux sysfs; [] elsewhere)."""
    devices = []
    try:
        entries = sorted(sys_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        vendor = _read_sysfs(entry / "idVendor")
        if not vendor:
            continue
        try:
            vendor_id = int(vendor, 16)
            product_id = int(_read_sysfs(entry / "idProduct") or "0", 16)
        except ValueError:
            continue
        if vendor_id != ELGATO_VENDOR_ID:
            continue
        hint = USB_MODEL_HINTS.get(product_id)
        product_name = _read_sysfs(entry / "product")
        serial = _read_sysfs(entry / "serial")
        model = hint[0] if hint else (product_name or f"Elgato device {product_id:04x}")
        devices.append({
            "id": stable_device_id(serial, model, entry.name),
            "model": model,
            "serial": serial,
            "usb_id": f"{vendor_id:04x}:{product_id:04x}",
            "usb_path": entry.name,
            "rows": hint[1] if hint else 0,
            "columns": hint[2] if hint else 0,
            "key_count": hint[1] * hint[2] if hint else 0,
            "known_model": bool(hint),
            "source": "usb",
        })
    return devices


def usb_signature(sys_root: Path = Path("/sys/bus/usb/devices")) -> Optional[str]:
    """Cheap fingerprint of attached Elgato devices; ``None`` when sysfs is unavailable."""
    if not sys_root.is_dir():
        return None
    return "|".join(sorted(f"{row['usb_path']}:{row['usb_id']}:{row['serial']}" for row in detect_usb_devices(sys_root)))
