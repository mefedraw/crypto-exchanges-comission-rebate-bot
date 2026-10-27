"""Compile reviewed PO catalogs. Requires the pinned Babel development dependency."""

from __future__ import annotations

from pathlib import Path

from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po


def main() -> None:
    root = Path(__file__).resolve().parents[1] / "bot" / "i18n" / "locales"
    for path in sorted(root.glob("*/LC_MESSAGES/messages.po")):
        with path.open("rb") as source:
            catalog = read_po(source, locale=path.parents[1].name)
        errors = list(catalog.check())
        if errors:
            raise ValueError(f"Invalid translations in {path}: {errors}")
        with path.with_suffix(".mo").open("wb") as compiled:
            write_mo(compiled, catalog)


if __name__ == "__main__":
    main()
