# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for CSV formula-injection neutralisation.

A serial device can emit a payload such as
``=HYPERLINK("http://attacker/",x)`` or ``=cmd|'/c calc'!A1``. Opened
in Excel or LibreOffice Calc, a field that starts with ``=``, ``+``,
``-``, or ``@`` is treated as a formula and executed. The trust path
runs device -> CSV -> spreadsheet -> analyst, so a malicious or buggy
device could run code on the user's machine.

``_csv_escape`` prefixes such a value with a single quote (OWASP CSV
Injection guidance) so the spreadsheet renders it as literal text. It
is the single chokepoint for both AUTO-mode and RAW-mode rows. A plain
signed number is exempt, because it cannot carry a formula.

The end-to-end tests parse the output back with the stdlib csv reader,
exactly how a spreadsheet tool reads the fields.
"""

from __future__ import annotations

import csv
import io

import pytest

from app.constants import CSVMode
from core.csv_engine import _CSV_FORMULA_TRIGGERS, CSVEngine

MALICIOUS_PAYLOADS = [
    '=HYPERLINK("http://example.com/","x")',
    "=cmd|'/c calc'!A1",
    "+cmd|'/c calc'!A1",
    "-2+3+cmd|'/c calc'!A1",
    "@SUM(1+1)",
]


class TestCsvEscapeNeutralisation:
    def test_each_trigger_char_is_prefixed(self):
        for trigger in _CSV_FORMULA_TRIGGERS:
            escaped = CSVEngine._csv_escape(f"{trigger}payload")
            assert escaped.lstrip('"').startswith("'")

    @pytest.mark.parametrize("payload", MALICIOUS_PAYLOADS)
    def test_formula_payload_gets_single_quote(self, payload):
        field = next(csv.reader(io.StringIO(CSVEngine._csv_escape(payload))))[0]
        assert field == "'" + payload

    @pytest.mark.parametrize(
        "number",
        ["-5.2", "+3", "-0.5", "-12", "+1e5", "-3.3E-2", "-.5", "+7."],
    )
    def test_plain_signed_number_is_left_as_a_number(self, number):
        assert CSVEngine._csv_escape(number) == number

    @pytest.mark.parametrize(
        "value",
        ["-5.2+1", "+8884445555x", "-", "+", "-1e", "--5", "-5.2.1", "-1 2"],
    )
    def test_value_that_only_resembles_a_number_is_neutralised(self, value):
        assert CSVEngine._csv_escape(value) == "'" + value

    def test_neutralise_then_rfc_quote_for_values_with_commas(self):
        assert CSVEngine._csv_escape("=A1,B1") == '"\'=A1,B1"'

    def test_benign_values_untouched(self):
        for value in ("22.4", "IDLE", "ok", "0x1F", "1e5"):
            assert CSVEngine._csv_escape(value) == value

    def test_empty_value(self):
        assert CSVEngine._csv_escape("") == ""

    def test_trigger_set_matches_owasp(self):
        assert set(_CSV_FORMULA_TRIGGERS) == {"=", "+", "-", "@", "\t", "\r"}


class TestRawModeNeutralisation:
    def test_malicious_lines_neutralised_in_raw_output(self):
        engine = CSVEngine()
        engine.detect_mode([])
        assert engine.mode == CSVMode.RAW
        for payload in MALICIOUS_PAYLOADS:
            row = engine.feed("2026-01-01 10:00:00.000", payload)[0]
            fields = next(csv.reader(io.StringIO(row)))
            assert fields[1] == "'" + payload


class TestAutoModeNeutralisation:
    def test_malicious_column_value_neutralised(self):
        engine = CSVEngine()
        engine.detect_mode([f"name=dev{i} cmd=run" for i in range(10)])
        assert engine.mode == CSVMode.AUTO
        row = engine.feed("T", "name=dev1 cmd==SUM(A1)")[0]
        fields = next(csv.reader(io.StringIO(row)))
        assert fields == ["T", "dev1", "'=SUM(A1)"]

    def test_negative_reading_stays_numeric_in_auto_output(self):
        engine = CSVEngine()
        engine.detect_mode([f"Temp: -{i}.5, Offset: +{i}" for i in range(10)])
        assert engine.feed("T", "Temp: -5.2, Offset: +3") == ["T,-5.2,+3"]
