# Bundled fonts

seendiff ships two monospace webfonts so the UI renders identically without a
network fetch. Both are subsetted and converted to woff2; neither is the
upstream file byte-for-byte.

| File | Font | Version | Licence |
|---|---|---|---|
| `JetBrainsMono.woff2` | JetBrains Mono Regular | 2.211 | SIL Open Font License 1.1 — [`LICENSE-JetBrainsMono.txt`](LICENSE-JetBrainsMono.txt) |
| `UbuntuSansMono.woff2` | Ubuntu Sans Mono Regular | 1.006 | Ubuntu Font Licence 1.0 — [`LICENCE-UbuntuSansMono.txt`](LICENCE-UbuntuSansMono.txt) |

Upstream sources:

- JetBrains Mono — <https://github.com/JetBrains/JetBrainsMono>
- Ubuntu Sans Mono — <https://github.com/canonical/Ubuntu-Sans-fonts>

## Modifications

Both files were reduced to the glyphs seendiff actually renders and converted
to woff2 (394 glyphs / 229 codepoints for JetBrains Mono, 259 / 227 for Ubuntu
Sans Mono). No outlines, metrics, or hinting were altered.

The JetBrains Mono copyright declares no Reserved Font Name, so the subset is
redistributable under the OFL as-is, provided this licence text travels with
it.

The Ubuntu Font Licence treats a format conversion as a Modified Version
(clause 2), which carries a naming requirement — see the licence text for the
exact terms.
