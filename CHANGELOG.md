# WireTrace Changelog

All notable changes to this project follow [Semantic Versioning](https://semver.org/).

---

## [1.1.0] — 2026-05-24

First feature release after v1.0.0. Adds the live data plotter,
manual plot configuration with named profiles, two data-protection
fixes, and parser improvements. Fully backwards-compatible: no API
breaks, no file-format breaks, no user action required to upgrade.

### Added — Live data plotter

- **Docked plot panel** beneath the console. Auto-detects three
  structured formats — JSON object lines, `key:value` pairs,
  positional values with auto-detected delimiter (comma, tab,
  semicolon, pipe, whitespace).
- **`plt:` / `plt0:` / `plt1:` prefix routing** for selectively
  plotting only tagged lines, and `plt!Name1,Name2,…` for explicit
  column naming.
- **Frequency-based detection** — a column must appear in ≥70% of
  matching sample lines, so boot banners, debug spam, and stray
  colons no longer corrupt detection.
- **Per-column ring buffer**, 10 000 samples × up to 8 traces, on
  `numpy.float64`. ~1.3 MB per tab.
- **Toolbar**: Pause / Clear / Reset View / Window (10 s / 30 s /
  60 s / 5 min / All) / Configure… / Legend toggle.
- **Splitter layout**: drag to resize, opens 50 / 50 by default,
  ratio remembered across toggles, plot collapses fully on hide.
- **`View → Live Plot`** menu item (`Ctrl+Shift+P`).
- **Theme-aware**: Okabe-Ito-derived 8-colour palette, Studio Light
  uses an amber instead of yellow for trace 7 (yellow-on-white was
  illegible). Switching themes mid-session retints traces, chrome,
  and legend without data loss.

### Added — Manual plot configuration

- **`View → Configure Plot…`** dialog (also reachable from the plot
  toolbar's **Configure…** button, the **`[ Configure manually… ]`**
  CTA shown when auto-detect gives up, and `File → Preferences →
  Plot`).
- **Two modes per tab**: Auto-detect (the default) and Manual.
- **Manual mode**: declare a regex with named groups; each
  `(?P<name>…)` becomes a plot column. Search-not-fullmatch so
  the pattern targets the payload, surrounding noise / timestamps /
  log prefixes don't break the match.
- **Capture from sample assistant**: pick a sample line, click each
  numeric value you want to plot, name it; the dialog scaffolds the
  regex for you. No need to hand-write regex unless you want to.
- **Tiered Test result**: Green (matched recent lines), Amber
  (pattern valid but no current matches — firmware may simply be
  quiet), Red (structurally broken). Apply is enabled for Green or
  Amber, disabled for Red. No "broken" UX state.
- **Named profiles**: save, rename, delete, set as default.
  Profiles persist in `preferences.ini` and survive app restarts.
  Built-in **Auto-detect** profile is protected from rename/delete.
- **Default profile** is applied automatically on new tabs and on
  reconnection so engineers don't have to re-enter their config
  every session.

### Changed — Data protection

- **fsync ordering on log stop.** `stop_logging()` now flushes →
  finalises CSV → fsyncs → closes. Previously the fsync happened
  before CSV finalisation, so the CSV's auto-detection sample
  buffer rows could be lost on a power cut between finalize and
  close. The window is now closed completely.
- **Periodic fsync during long sessions.** A 30-second fsync timer
  runs alongside the existing 1-second flush timer. Worst-case
  data loss on a hard kill (kernel panic, power loss, force-quit)
  is now bounded to 30 s of in-flight buffer, instead of the
  entire session.

### Changed — Tag detector & CSV

- Tag detector uses word-boundary regex per severity to eliminate
  false positives like *"default"* matching *"fault"*.
- CSV auto-detection samples only DATA-tagged rows; severity and
  command lines no longer pollute the schema and are routed only
  to the .txt log.

### Fixed

- **Status bar metrics reset on disconnect** — port, baud, data rate,
  and line count no longer show stale values from the previous
  session. This affected v1.0.0 too; the bar now clears cleanly when
  a device disconnects, regardless of how the disconnect happened
  (user-initiated, cable pull, or remote close).

### Discoverability

- Tooltips added to hidden plot affordances: pan/zoom on plot main
  area and per-axis, right-click context menu, legend click-to-
  toggle (each legend entry's coloured sample toggles that trace's
  visibility), splitter handle resize, and the Window dropdown's
  view-filter semantics. No popups, no first-run tour — quiet
  hover hints on the controls where the affordance isn't obvious.
- **User Guide rewritten and expanded** for v1.1.0. Covers the live
  plotter, manual configuration, the capture-from-sample assistant,
  tiered Green/Amber/Red Test result, saved profiles, the
  disconnect/reconnect contract, and the data-integrity model
  (buffering + periodic fsync). Adds a Plot section to Preferences,
  a Sessions section, and plot-specific troubleshooting entries.
- The User Guide's HTML is version-stamped at load time so the
  documentation a user reads always matches the executable they're
  running. The loader writes the stamped copy to the per-user cache
  directory (never inside the install tree) and gracefully handles
  Nuitka standalone, PyInstaller, and macOS .app bundle layouts.

### Dependencies

- New runtime: `pyqtgraph >= 0.13`, `numpy >= 1.24`.

### Test ledger

- v1.0.0 baseline: 85 tests
- v1.1.0 release: **327 tests**, ruff clean on Linux, macOS, and
  Windows (the CI matrix runs all three)
- Added: plot engine (auto + manual modes, late-bind,
  connect-disconnect-reconnect cycle, cross-platform timestamp
  precision), RegexParser (compile validation, partial-match,
  named-group extraction), PlotProfileStore (round-trip, corruption
  recovery, built-in protection), Configure Plot dialog tokenizer
  + pattern generation, log-engine fsync ordering, tag detector
  word-boundary regression (including the documented
  prefix-tolerant behaviour for "failed" / "warning" /
  "informational"), legend lifecycle across reconnect cycles,
  plot visibility signal contract, Preferences plot-section
  round-trip, console font-flow regression, Help loader
  cross-platform path resolution and version-stamping, and
  build-pipeline structural tests that guard against silent
  regressions in the Nuitka flags and the post-build smoke test.

---

## [1.0.0] — 2026-04-15

Initial public release of WireTrace. Multi-device serial monitor
with buffered logging, structured CSV export, search and live
filter, severity classification, and the Studio Light / Midnight
Dark themes. Cross-platform; pre-built Windows installer published
on GitHub Releases.
