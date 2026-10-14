"""Adapter registry. Adding an emulator means adding a module and one line here."""

from typing import Dict, List

from ..environment import SystemProbe
from .base import EmulatorAdapter
from .detect_only import build_detect_only_adapters
from .dolphin import DolphinAdapter
from .lan import GenericLanAdapter
from .ppsspp import PPSSPPAdapter
from .retroarch import RetroArchAdapter
from .streaming import StreamingAdapter
from .switch import SwitchLdnAdapter


def build_adapters(probe: SystemProbe) -> List[EmulatorAdapter]:
    return [
        RetroArchAdapter(probe),
        DolphinAdapter(probe),
        PPSSPPAdapter(probe),
        SwitchLdnAdapter(probe),
        GenericLanAdapter(probe),
        *build_detect_only_adapters(probe),
        StreamingAdapter(probe),
    ]


def adapters_by_id(adapters: List[EmulatorAdapter]) -> Dict[str, EmulatorAdapter]:
    return {adapter.id: adapter for adapter in adapters}
