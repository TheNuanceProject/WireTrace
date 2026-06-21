# SPDX-License-Identifier: MIT
"""Regression tests for B2 — CSV formula injection.

The bug: WireTrace's CSV export applied only RFC 4180 structural escaping
(commas, quotes, newlines). A serial device could emit a payload such as
``=HYPERLINK("http://attacker/",x)`` or ``=cmd|'/c calc'!A1`` and, when
the exported CSV was opened in Excel or LibreOffice Calc, the spreadsheet
would interpret the leading ``=`` / ``+`` / ``-`` / ``@`` as a formula
and execute it. The trust path runs device → CSV → spreadsheet → analyst,
so a malicious or buggy device could run code on the user's machine.

The fix neutralises formula triggers in ``_csv_escape`` (OWASP CSV
Injection guidance): a value whose first character is ``=``, ``+``,
``-``, ``@``, tab, or carriage return is prefixed with a single quote so
the spreadsheet renders it as literal text. ``_csv_escape`` is the single
chokepoint for both AUTO-mode and RAW-mode writes, so both are covered.

These tests pin the helper directly and verify both export modes
end-to-end, parsing the output back with the stdlib csv reader — exactly
how a spreadsheet tool reads the fields.
"""

from __future__ import annotations

import csv
import io

from app.constants import TAG_DATA, CSVMode
from core.csv_engine import _CSV_FORMULA_TRIGGERS, CSVEngine

# The canonical malicious payloads from the audit / VERIFY S-2.
MALICIOUS_PAYLOADS = [
    '=HYPERLINK("http://example.com/","x")',
    "=cmd|'/c calc'!A1",
    "+8884445555",
    "-0.5",
    "@SUM(1+1)",
]


class TestCsvEscapeNeutralisation:
    """Direct unit tests for the escape chokepoint."""

    def test_each_trigger_char_is_prefixed(self):
        for trigger in ("=", "+", "-", "@", "\t", "\r"):
            out = CSVEngine._csv_escape(f"{trigger}danger")
            # The neutralised value is the single quote followed by the
            # original (possibly then RFC-wrapped). It must start with a
            # quote-prefixed form, never the bare trigger.
            assert not out.startswith(trigger)

    def test_formula_payload_gets_single_quote(self):
        assert CSVEngine._csv_escape("=cmd|'/c calc'!A1") == "'=cmd|'/c calc'!A1"
        assert CSVEngine._csv_escape("@SUM(1+1)") == "'@SUM(1+1)"
        assert CSVEngine._csv_escape("+8884445555") == "'+8884445555"

    def test_leading_dash_number_is_neutralised(self):
        # Documented tradeoff: a value that is a legitimate negative
        # number also gets the quote prefix, because Excel treats a
        # leading '-' as a formula start. This matches VERIFY S-2.
        assert CSVEngine._csv_escape("-0.5") == "'-0.5"

    def test_neutralise_then_rfc_quote_for_values_with_commas(self):
        # A payload containing a comma is first prefixed, then RFC-wrapped
        # in quotes with internal quotes doubled. A csv reader unwraps it
        # back to a single-quote-prefixed literal.
        out = CSVEngine._csv_escape('=HYPERLINK("http://example.com/","x")')
        # Round-trip through a csv parser to verify the cell a spreadsheet
        # would see starts with the neutralising quote.
        cell = next(csv.reader([out]))[0]
        assert cell == '\'=HYPERLINK("http://example.com/","x")'
        assert not cell.startswith("=")

    def test_benign_values_untouched(self):
        assert CSVEngine._csv_escape("1450") == "1450"
        assert CSVEngine._csv_escape("RPM is fine") == "RPM is fine"
        assert CSVEngine._csv_escape("3.2") == "3.2"

    def test_empty_value(self):
        assert CSVEngine._csv_escape("") == ""

    def test_trigger_set_matches_owasp(self):
        assert set(_CSV_FORMULA_TRIGGERS) == {"=", "+", "-", "@", "\t", "\r"}


def _read_cells(csv_text: str) -> list[list[str]]:
    """Parse CSV output the way a spreadsheet would, returning rows of
    cells (header included)."""
    return list(csv.reader(io.StringIO(csv_text)))


class TestRawModeNeutralisation:
    """RAW mode writes the whole line verbatim through _csv_escape."""

    def test_malicious_lines_neutralised_in_raw_output(self):
        engine = CSVEngine()
        buf = io.StringIO()
        ts = "2026-01-01 10:00:00"
        # Unstructured malicious lines → detection lands on RAW.
        for payload in MALICIOUS_PAYLOADS:
            engine.write_row(buf, ts, payload, TAG_DATA)
        engine.finalize(buf)

        rows = _read_cells(buf.getvalue())
        assert rows[0] == ["Timestamp", "Data"]
        data_cells = [row[1] for row in rows[1:]]
        # Every malicious value, as a spreadsheet would read it, starts
        # with the neutralising quote and not a formula trigger.
        assert len(data_cells) == len(MALICIOUS_PAYLOADS)
        for cell in data_cells:
            assert cell.startswith("'"), cell
            assert not cell.startswith(("=", "+", "-", "@", "\t", "\r"))


class TestAutoModeNeutralisation:
    """AUTO mode neutralises extracted column values too."""

    def test_malicious_column_value_neutralised(self):
        engine = CSVEngine()
        buf = io.StringIO()
        ts = "2026-01-01 10:00:00"
        # Consistent key:value structure → AUTO mode, column 'v'.
        # One row carries a formula payload as its value.
        rows_in = [
            "v:1",
            "v:2",
            "v:=2+2",
            "v:4",
        ]
        for line in rows_in:
            engine.write_row(buf, ts, line, TAG_DATA)
        engine.finalize(buf)

        assert engine.mode == CSVMode.AUTO
        assert engine.columns == ["v"]

        rows = _read_cells(buf.getvalue())
        assert rows[0] == ["Timestamp", "v"]
        values = [row[1] for row in rows[1:]]
        assert "'=2+2" in values, values
        for cell in values:
            assert not cell.startswith(("=", "+", "-", "@", "\t", "\r"))
