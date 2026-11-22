"""A hostile archived gamelist must not smuggle entity definitions past the DOCTYPE filter.

Issue #760: "Older backups fall back to archived `roms/<system>/gamelist.xml` `<path>`
entries (`assets.legacy_from_gamelists`, `xml.etree` rejecting `DOCTYPE`/`ENTITY`)."

`legacy_from_gamelists` scans the raw member bytes for the ASCII substrings
`<!DOCTYPE`/`<!ENTITY` and then hands the bytes to `xml.etree`, which expands
internal entities. A gamelist encoded as UTF-16 (BOM + encoding declaration)
contains no such ASCII substring, so the filter passes while the parser still
honours theSubset and expands the entities -- a paired peer's backup archive
turns an admin's "Get missing games" plan into attacker-controlled entity
expansion (memory/CPU DoS). The file must be skipped in any encoding.
"""

import io
import tarfile
import unittest

from app.backups import assets


def _member(name, data):
    info = tarfile.TarInfo(name)
    info.size = len(data)
    return info, io.BytesIO(data)


class LegacyGamelistEncodingBypassTests(unittest.TestCase):
    def test_utf16_gamelist_with_doctype_is_rejected(self):
        bomb = (
            '<?xml version="1.0" encoding="UTF-16"?>'
            '<!DOCTYPE lolz [<!ENTITY a "AAAAAAAAAA"><!ENTITY b "&a;&a;&a;&a;&a;">]>'
            '<gameList><game><path>./Evil.sfc</path><name>&b;</name></game></gameList>'
        ).encode("utf-16")
        # Sanity: the current byte filter really is blind to this encoding.
        self.assertNotIn(b"<!DOCTYPE", bomb.upper())
        members = [_member("data/roms/snes/gamelist.xml", bomb)]
        document = assets.legacy_from_gamelists(members)
        self.assertEqual(document.get("roms"), [])

    def test_utf8_gamelist_with_doctype_is_rejected_control(self):
        bomb = (
            b'<?xml version="1.0"?>'
            b'<!DOCTYPE lolz [<!ENTITY a "AAAAAAAAAA">]>'
            b'<gameList><game><path>./Evil.sfc</path><name>&a;</name></game></gameList>'
        )
        members = [_member("data/roms/snes/gamelist.xml", bomb)]
        document = assets.legacy_from_gamelists(members)
        self.assertEqual(document.get("roms"), [])


if __name__ == "__main__":
    unittest.main()
