# SPDX-License-Identifier: MIT
"""WireTrace live-plot parsers.

A small pipeline of stateful parsers that detects numeric column
structure in a sample of serial lines, then extracts numeric values
per line for plotting.

Auto-detect pipeline (in priority order):
  1. JsonParser       — JSON object lines: ``{"temp": 25, "hum": 55}``
  2. KvParser         — key:value or key=value pairs:
                        ``Temperature: 22.4, Humidity: 55``
  3. DelimitedParser  — positional values with auto-detected delimiter
                        (comma, tab, semicolon, pipe, or whitespace).
                        Columns auto-named ``ch0``, ``ch1``, … unless a
                        ``plt!`` config line was seen first.

Manual mode (escape hatch for firmware that the auto-detect pipeline
can't recognise):
  4. RegexParser      — user-declared regex with named groups; each
                        ``(?P<name>…)`` becomes a plot column. The
                        engineer always wins — if they know the format,
                        they declare it. Coexists with the pipeline:
                        PlotEngine swaps between AUTO and MANUAL modes
                        via ``set_auto_config`` / ``set_manual_config``.

Routing layer (above the parsers):
  ParserPipeline understands a ``plt:`` / ``plt0:`` / ``plt1:`` prefix
  protocol. Lines with this prefix are routed to the plotter regardless
  of whether other lines in the stream match a structure. A ``plt!``
  config line provides explicit column names for positional data.

Design principles:
  - Every parser implements the same minimal Parser protocol
  - Parsers are stateful (own their detected columns / delimiter)
  - All parsers return only NUMERIC values — non-numeric values
    cause the line to be rejected
  - No I/O, no GUI, no threading — pure data
"""

from __future__ import annotations

import json
import re
from collections import Counter
from typing import ClassVar, Protocol

# ── Helpers ──────────────────────────────────────────────────────────────────

def _try_parse_float(s: str) -> float | None:
    """Return ``float(s)`` if it parses, else None. Never raises."""
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


#: Minimum fraction of MATCHING sample lines a key must appear in to be
#: accepted as a detected column. Strict intersection (100%) is brittle:
#: real-world serial streams often mix boot banners with telemetry, and
#: rare debug lines produce stray KV-like tokens that collapse the
#: intersection. 70% keeps stable telemetry with occasional dropouts
#: while excluding noise that appears in just a handful of lines.
_KEY_FREQ_THRESHOLD = 0.7


# ── Parser Protocol ──────────────────────────────────────────────────────────

class Parser(Protocol):
    """Live-plot parser interface.

    Each parser is instantiated once per session, called ``detect`` once
    on a sample of lines, then ``extract`` once per subsequent line.
    Implementations hold their own state (detected columns, delimiter,
    etc.) between the two calls.
    """

    name: ClassVar[str]

    @property
    def columns(self) -> list[str]:
        """Detected column names. Empty list before detection."""

    def detect(self, sample: list[str]) -> list[str] | None:
        """Try to detect numeric column structure from a sample.

        Returns column names if structure was detected (and stores any
        internal state needed for ``extract`` to work). Returns None if
        no structure was found.
        """

    def extract(self, line: str) -> dict[str, float] | None:
        """Extract numeric values from a single line.

        Returns ``dict[col_name → float]`` on success, None if the line
        doesn't conform to the detected structure or contains no
        numeric values.
        """

    def set_column_names(self, names: list[str]) -> None:
        """Override column names (used by the prefix protocol's
        ``plt!`` config line). May be a no-op for parsers where columns
        are name-keyed (JSON, KV); meaningful for positional formats."""


# ── JSON Parser ──────────────────────────────────────────────────────────────

class JsonParser:
    """Parses JSON-object lines, e.g. ``{"temp": 22.4, "hum": 55}``.

    Detection requires at least 50% of sample lines to be valid JSON
    objects with a non-empty intersection of numeric-valued keys.
    Boolean values are excluded (Python's ``bool`` is a subclass of
    ``int`` but a true/false toggle is not a plottable signal).
    """

    name: ClassVar[str] = "json"

    def __init__(self) -> None:
        self._columns: list[str] = []

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    def detect(self, sample: list[str]) -> list[str] | None:
        if not sample:
            return None

        # Per matching line: ordered list of numeric-valued keys
        per_line_keys: list[list[str]] = []

        for line in sample:
            stripped = line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                obj = json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                continue
            if not isinstance(obj, dict) or not obj:
                continue

            ordered_numeric: list[str] = []
            for k, v in obj.items():
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)) or (isinstance(v, str) and _try_parse_float(v) is not None):
                    ordered_numeric.append(k)

            if ordered_numeric:
                per_line_keys.append(ordered_numeric)

        if not per_line_keys or len(per_line_keys) < len(sample) * 0.5:
            return None

        # Frequency-based detection: keys appearing in >= 70% of
        # matching lines are accepted as columns.
        freq: Counter[str] = Counter()
        for keys in per_line_keys:
            freq.update(set(keys))
        threshold = len(per_line_keys) * _KEY_FREQ_THRESHOLD
        common = {k for k, c in freq.items() if c >= threshold}

        if not common:
            return None

        # Order preservation: prefer the first line that contains every
        # common key, so the detected order matches the device's
        # actual emission order.
        for keys in per_line_keys:
            if all(k in keys for k in common):
                self._columns = [k for k in keys if k in common]
                return list(self._columns)
        # Fallback: first line's keys, then any missing common keys
        # in stable (alphabetical) order.
        first = per_line_keys[0]
        ordered = [k for k in first if k in common]
        for k in sorted(common):
            if k not in ordered:
                ordered.append(k)
        self._columns = ordered
        return list(self._columns)

    def extract(self, line: str) -> dict[str, float] | None:
        if not self._columns:
            return None
        stripped = line.strip()
        if not stripped.startswith("{"):
            return None
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(obj, dict):
            return None

        result: dict[str, float] = {}
        for col in self._columns:
            if col not in obj:
                continue
            v = obj[col]
            if isinstance(v, bool):
                continue
            if isinstance(v, (int, float)):
                result[col] = float(v)
            elif isinstance(v, str):
                f = _try_parse_float(v)
                if f is not None:
                    result[col] = f
        return result if result else None

    def set_column_names(self, names: list[str]) -> None:
        # JSON is keyed by name, not position — overrides don't apply.
        # No-op preserves the protocol contract without changing behaviour.
        del names


# ── Key/Value Parser ─────────────────────────────────────────────────────────

class KvParser:
    """Parses key:value or key=value pairs with numeric values.

    Examples:
      ``Temperature: 22.4, Humidity: 55, Pressure: 1013``
      ``voltage=3.3 current=0.05``

    Detection requires at least 50% of sample lines to contain at
    least one consistent KV pair across the sample (set intersection
    of keys per line).
    """

    name: ClassVar[str] = "kv"

    # Key: word characters, possibly with internal spaces.
    # Value: optional sign, digits with optional decimal/exponent.
    _KV_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"(\w[\w\s]*?)\s*[:=]\s*([+-]?\d+\.?\d*(?:[eE][+-]?\d+)?)",
    )

    def __init__(self) -> None:
        self._columns: list[str] = []

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    def detect(self, sample: list[str]) -> list[str] | None:
        if not sample:
            return None

        per_line_keys: list[list[str]] = []
        for line in sample:
            matches = self._KV_PATTERN.findall(line)
            if matches:
                per_line_keys.append([k.strip() for k, _ in matches])

        if not per_line_keys or len(per_line_keys) < len(sample) * 0.5:
            return None

        # Frequency-based detection: keys appearing in >= 70% of
        # matching lines are accepted as columns. This rejects boot
        # banners and stray debug lines whose keys appear only once or
        # twice while keeping stable telemetry with sporadic dropouts.
        freq: Counter[str] = Counter()
        for keys in per_line_keys:
            freq.update(set(keys))
        threshold = len(per_line_keys) * _KEY_FREQ_THRESHOLD
        common = {k for k, c in freq.items() if c >= threshold}

        if not common:
            return None

        # Order preservation: prefer the first line that contains every
        # common key.
        for keys in per_line_keys:
            if all(k in keys for k in common):
                self._columns = [k for k in keys if k in common]
                return list(self._columns)
        # Fallback: first line's keys, then any missing common keys
        # in stable (alphabetical) order.
        first = per_line_keys[0]
        ordered = [k for k in first if k in common]
        for k in sorted(common):
            if k not in ordered:
                ordered.append(k)
        self._columns = ordered
        return list(self._columns)

    def extract(self, line: str) -> dict[str, float] | None:
        if not self._columns:
            return None
        matches = self._KV_PATTERN.findall(line)
        if not matches:
            return None
        result: dict[str, float] = {}
        for key, val_str in matches:
            key = key.strip()
            if key in self._columns:
                f = _try_parse_float(val_str)
                if f is not None:
                    result[key] = f
        return result if result else None

    def set_column_names(self, names: list[str]) -> None:
        # KV is keyed by name, not position — overrides don't apply.
        del names


# ── Delimited / Positional Parser ────────────────────────────────────────────

class DelimitedParser:
    """Parses positional numeric values with an auto-detected delimiter.

    Tries comma, tab, semicolon, pipe, and any-whitespace in order.
    A delimiter wins if at least 50% of sample lines split into the
    same count (>= 2) of all-numeric tokens. Explicit delimiters beat
    whitespace on ties to favor structured CSV-style streams.

    Columns are auto-named ``ch0``, ``ch1``, … by default. The pipeline
    can call ``set_column_names`` to override these (used when a
    ``plt!Name1,Name2,…`` config line is detected upstream).
    """

    name: ClassVar[str] = "delimited"

    # Explicit single-char delimiters in priority order.
    _DELIMITERS: ClassVar[tuple[str, ...]] = (",", "\t", ";", "|")
    # Sentinel for "any-whitespace" mode (lower priority).
    _WHITESPACE: ClassVar[str] = "\x00WS\x00"

    def __init__(self) -> None:
        self._columns: list[str] = []
        self._delimiter: str | None = None  # None = whitespace, else explicit

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    def detect(self, sample: list[str]) -> list[str] | None:
        if not sample:
            return None

        candidates: list[str] = [*self._DELIMITERS, self._WHITESPACE]
        # best is (votes, prefer_explicit, delim, count)
        best: tuple[int, int, str, int] | None = None

        for delim in candidates:
            split_lines: list[list[str]] = []
            for line in sample:
                tokens = self._split(line, delim)
                if len(tokens) < 2:
                    continue
                if any(t == "" for t in tokens):
                    continue
                if not all(_try_parse_float(t) is not None for t in tokens):
                    continue
                split_lines.append(tokens)

            if len(split_lines) < len(sample) * 0.5:
                continue

            # Most common token count wins for this delimiter
            counts = Counter(len(tokens) for tokens in split_lines)
            most_common_count, votes = counts.most_common(1)[0]
            if most_common_count < 2:
                continue
            if votes < len(sample) * 0.5:
                continue

            prefer_explicit = 0 if delim == self._WHITESPACE else 1
            score = (votes, prefer_explicit, delim, most_common_count)
            if best is None or score > best:
                best = score

        if best is None:
            return None

        _, _, delim, count = best
        self._delimiter = None if delim == self._WHITESPACE else delim
        self._columns = [f"ch{i}" for i in range(count)]
        return list(self._columns)

    def extract(self, line: str) -> dict[str, float] | None:
        if not self._columns:
            return None
        tokens = self._split(line, self._delimiter or self._WHITESPACE)
        if len(tokens) != len(self._columns):
            return None
        result: dict[str, float] = {}
        for col, tok in zip(self._columns, tokens, strict=False):
            if tok == "":
                return None
            f = _try_parse_float(tok)
            if f is None:
                return None
            result[col] = f
        return result

    def set_column_names(self, names: list[str]) -> None:
        # Positional — overrides apply if the count matches.
        if len(names) == len(self._columns):
            self._columns = list(names)

    @staticmethod
    def _split(line: str, delim: str) -> list[str]:
        if delim == DelimitedParser._WHITESPACE:
            return line.split()
        return [t.strip() for t in line.split(delim)]


# ── Regex Parser (manual mode) ───────────────────────────────────────────────

class RegexParserError(ValueError):
    """Raised when a regex pattern is invalid for use as a plot parser.

    Distinct from ``re.error`` so callers can distinguish "regex didn't
    compile" (still ``re.error``, wrapped as ``cause``) from "regex
    compiled but is unusable for plotting" (no named groups).
    """


class RegexParser:
    """User-declared regex with named groups → plot columns.

    The manual-mode parser. The user supplies a regex pattern; each
    ``(?P<name>...)`` named group becomes a plot column. On every
    line, ``re.search`` runs and any group whose captured text parses
    as a float contributes a sample.

    Why ``re.search`` and not ``re.fullmatch``:
      Real serial output mixes payload and noise. The engineer's
      pattern targets the payload; expecting them to anchor the
      pattern with ``^…$`` and account for prefixes/timestamps would
      be hostile UX. Using ``search`` lets a tight pattern like
      ``RPM:\\s*(?P<RPM>\\d+)`` match the relevant span anywhere in
      the line.

    Why named groups only:
      Plot columns need names. Numbered groups (``(\\d+)``) are
      ambiguous to label and can change meaning silently if the user
      reorders. Named groups make intent explicit and self-documenting.

    Why values must be numeric:
      Plotting non-numeric data is meaningless. A named group that
      captures non-numeric text on a given line causes that line to
      be skipped (not the whole pattern rejected) — firmware that
      occasionally emits ``RPM: ERR`` keeps plotting on the lines
      where ``RPM`` is a number.
    """

    name: ClassVar[str] = "regex"

    def __init__(self, pattern: str) -> None:
        if not isinstance(pattern, str) or not pattern.strip():
            raise RegexParserError("Pattern is empty.")
        try:
            self._compiled = re.compile(pattern)
        except re.error as exc:
            raise RegexParserError(
                f"Pattern failed to compile: {exc}",
            ) from exc

        # Named groups in declaration order. ``groupindex`` is dict-
        # like {name: index}; sorting by index gives declaration order.
        named = sorted(
            self._compiled.groupindex.items(), key=lambda kv: kv[1],
        )
        if not named:
            raise RegexParserError(
                "Pattern has no named groups. "
                "Use (?P<name>...) for each value you want to plot.",
            )

        self._pattern = pattern
        self._columns = [name for name, _ in named]

    @property
    def pattern(self) -> str:
        return self._pattern

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    def extract(self, line: str) -> dict[str, float] | None:
        """Extract numeric named-group captures from ``line``.

        Returns ``None`` if the pattern doesn't match, or if no named
        group captured a numeric value. Returns a partial dict (only
        the columns that captured numerics) if some groups matched
        non-numerics — keeps plotting alive when one channel emits
        ``ERR`` while others stay healthy.
        """
        match = self._compiled.search(line)
        if match is None:
            return None
        out: dict[str, float] = {}
        for name in self._columns:
            captured = match.group(name)
            if captured is None:
                continue
            v = _try_parse_float(captured.strip())
            if v is not None:
                out[name] = v
        return out if out else None

    def test(self, sample: list[str]) -> dict[str, object]:
        """Run the pattern across a sample and report results.

        Used by the Configure Plot dialog's Test button. Returns a
        dict with keys:
          - ``matched`` (int):  count of lines that produced ≥1 numeric
          - ``total`` (int):    sample size
          - ``columns`` (list): column names emitted by at least one
                                line in the sample
          - ``preview`` (list): first three matching extractions
                                (line, dict[name, float])
        """
        matched = 0
        seen_columns: set[str] = set()
        preview: list[tuple[str, dict[str, float]]] = []
        for line in sample:
            values = self.extract(line)
            if values:
                matched += 1
                seen_columns.update(values.keys())
                if len(preview) < 3:
                    preview.append((line, values))
        return {
            "matched": matched,
            "total": len(sample),
            "columns": [c for c in self._columns if c in seen_columns],
            "preview": preview,
        }

    def set_column_names(self, names: list[str]) -> None:
        # RegexParser column names come from the pattern's named groups
        # and are not user-overridable post-construction. The Parser
        # protocol declares this method so we keep a no-op for shape
        # compatibility.
        return


# ── Pipeline ─────────────────────────────────────────────────────────────────

class ParserPipeline:
    """Orchestrates parser detection and extraction with prefix routing.

    Detection flow:
      1. Scan the sample for ``plt!Name1,Name2,…`` config lines and
         capture the column-name override (first one wins).
      2. Decide the operating mode:
         - PREFIX MODE if any sample line starts with ``plt:`` /
           ``plt0:`` / ``plt1:`` / etc. Only prefixed lines are
           considered for detection and ongoing plotting; the
           prefix is stripped before the inner parsers see the line.
         - AUTO MODE otherwise. All sample lines are considered.
      3. Try each parser in priority order. The first one that
         succeeds wins.
      4. If a column-name override was captured AND the count matches,
         apply it to the chosen parser via ``set_column_names``.

    Extraction flow:
      - In PREFIX MODE: lines without a prefix return None (not for
        plot). Lines with a prefix have it stripped before extraction.
      - ``plt!`` lines after detection return None (already consumed).
      - Otherwise the chosen parser handles the line.
    """

    # Routing prefix: ``plt`` followed by optional digits, then ``:``
    _PREFIX_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^plt\d*\s*:\s*", re.IGNORECASE,
    )
    # Config: ``plt!`` followed by optional digits, then ``!`` and a
    # comma-separated list of column names.
    _CONFIG_PATTERN: ClassVar[re.Pattern[str]] = re.compile(
        r"^plt\d*\s*!\s*(.+?)\s*$", re.IGNORECASE,
    )

    def __init__(self) -> None:
        self._parsers: list[Parser] = [
            JsonParser(),
            KvParser(),
            DelimitedParser(),
        ]
        self._active: Parser | None = None
        self._prefix_mode = False
        self._column_names_override: list[str] | None = None
        self._columns: list[str] = []

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    @property
    def detected(self) -> bool:
        return self._active is not None

    @property
    def active_parser_name(self) -> str | None:
        return self._active.name if self._active else None

    @property
    def prefix_mode(self) -> bool:
        return self._prefix_mode

    def detect(self, sample: list[str]) -> list[str] | None:
        # Pass 1: capture any plt! config line (first wins)
        for line in sample:
            m = self._CONFIG_PATTERN.match(line.strip())
            if m:
                self._column_names_override = [
                    n.strip() for n in m.group(1).split(",") if n.strip()
                ]
                break

        # Pass 2: decide prefix vs auto mode
        prefixed_payloads: list[str] = []
        for line in sample:
            stripped = line.strip()
            if self._CONFIG_PATTERN.match(stripped):
                continue  # config line — don't feed to parsers
            payload = self._strip_prefix(stripped)
            if payload is not None:
                prefixed_payloads.append(payload)

        if prefixed_payloads:
            self._prefix_mode = True
            inner_sample = prefixed_payloads
        else:
            self._prefix_mode = False
            inner_sample = [
                line for line in sample
                if not self._CONFIG_PATTERN.match(line.strip())
            ]

        # Pass 3: try parsers in priority order
        for parser in self._parsers:
            cols = parser.detect(inner_sample)
            if cols is not None:
                if self._column_names_override and \
                        len(self._column_names_override) == len(cols):
                    parser.set_column_names(self._column_names_override)
                    cols = parser.columns
                self._active = parser
                self._columns = list(cols)
                return list(self._columns)

        return None

    def extract(self, line: str) -> dict[str, float] | None:
        if self._active is None:
            return None

        # Config lines after detection are silently consumed (no plot).
        if self._CONFIG_PATTERN.match(line.strip()):
            return None

        if self._prefix_mode:
            payload = self._strip_prefix(line.strip())
            if payload is None:
                return None  # unprefixed line — not for plot
            return self._active.extract(payload)

        return self._active.extract(line)

    @classmethod
    def _strip_prefix(cls, line: str) -> str | None:
        """Return the payload after a ``plt:`` / ``plt0:`` prefix, or
        None if the line has no plot prefix."""
        m = cls._PREFIX_PATTERN.match(line)
        if m is None:
            return None
        return line[m.end():]
