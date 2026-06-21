# WireTrace Changelog

All notable changes to this project follow [Semantic Versioning](https://semver.org/).

---

## [1.2.0] — 2026-06-21

Maintenance release. Hardens data durability, fixes disconnect handling
on Linux and a plot-pattern hang, and corrects window restore after a
monitor change. Linux binaries (AppImage and `.deb`) are now published,
with working in-app updates. Fully backwards-compatible: no API breaks,
no file-format breaks, no user action required to upgrade.

### Fixed

- **Atomic preferences save.** `preferences.ini` is now written to a
  temporary file and atomically renamed into place, with an fsync
  before the rename. A crash, power loss, or full disk during a save
  can no longer leave the file empty or truncated. Previously an
  interrupted save could wipe all preferences and saved plot profiles,
  with the next launch silently falling back to defaults. Affects
  v1.0.0 and v1.1.0.
- **ANSI/VT100 escape codes stripped from output.** Lines from firmware
  that emits colour or cursor-control sequences (RTOS shells, U-Boot,
  coloured CLIs) are now cleaned at the read layer, so the console, the
  disk log, the CSV export, and the plot parsers all receive plain
  text. Colour-wrapped numbers now parse for plotting, and a line that
  was only escape codes (such as a clear-screen sequence) is dropped
  rather than logged as noise. Stripping only — terminal rendering
  remains out of scope. Affects v1.0.0 and v1.1.0.
- **CSV exports hardened against formula injection.** Exported values
  whose first character is one a spreadsheet treats as a formula start
  (`=`, `+`, `-`, `@`, tab, or carriage return) are now prefixed with a
  single quote, so opening a log in Excel or LibreOffice Calc renders
  the value as literal text instead of executing it. This closes a path
  where a malicious or buggy device could run code on the machine of
  anyone who opened the CSV. Applied to both auto-detected and raw
  exports. Affects v1.0.0 and v1.1.0.
- **Serial port released immediately on disconnect (Linux).** When a USB
  serial device is unplugged or otherwise becomes unavailable, the port
  is now closed as soon as the resource error is detected, so the kernel
  releases the tty node right away. Previously the node stayed locked and
  the device could re-enumerate to a different path (for example
  `/dev/ttyACM1` instead of `/dev/ttyACM0`) on reconnect, breaking
  automatic reconnection to the same port. Other error types are
  unaffected. Affects v1.0.0 and v1.1.0.
- **Plot view releases its theme subscription on close.** Closing a tab
  now disconnects the plot view from the theme manager, so the view is
  freed instead of being held alive by the long-lived theme manager.
  This removes a small memory growth across repeated tab closes and a
  latent risk of updating a closed view when the theme changed. Affects
  v1.1.0.
- **Reader and log-engine signals released on disconnect.** Disconnecting
  a device now detaches every signal connected to the reader and log
  engine for that session, not just the data relay. Previously the
  remaining connections kept the finished reader and log-engine instances
  alive, so a session that repeatedly disconnected and reconnected (the
  hardware bring-up workflow) accumulated their buffers in memory. The
  final flushed line and last rate update on disconnect are still
  delivered. Affects v1.0.0 and v1.1.0.
- **Log files protected against concurrent-write corruption.** All writes,
  fsyncs, and closes of an active log file now happen under a single lock,
  so the periodic flush running on the writer thread can no longer
  interleave bytes with the final flush triggered when logging stops.
  Previously a narrow timing window during shutdown could produce a
  garbled or truncated final line in the log. Affects v1.0.0 and v1.1.0.
- **Plot regex patterns can no longer freeze the app.** Custom plot
  patterns entered in Configure Plot now run with a short per-line time
  limit. A pattern that triggers catastrophic backtracking on a line is
  treated as a non-match and the line is skipped, so the parsing thread
  and the UI stay responsive instead of locking up. The Configure Plot
  Test button reports a timed-out pattern in red and keeps Apply
  disabled until the pattern is changed. Affects v1.1.0.
- **pyserial fallback stops cleanly on disconnect.** On the rare hardware
  path where WireTrace reads a port through the pyserial fallback instead
  of Qt, disconnecting no longer lets the background reader deliver one
  last data signal after shutdown had already begun. The data path is now
  guarded the same way the error path already was. Affects v1.0.0 and
  v1.1.0.
- **Linux AppImage now bundles its dependencies.** The Linux build
  previously shipped just the launcher executable renamed to
  `.AppImage`, leaving behind the Qt libraries (including shiboken6) and
  every other dependency, so it could not start. The build now packages
  the complete application into the AppImage — or, if the AppImage tool
  is unavailable, a tarball of the full directory — so the artifact runs
  on its own. Affects v1.1.0.
- **The tab now updates when a device drops on its own.** When a device
  was unplugged or the connection was lost, the port was released and the
  status bar said "Disconnected," but the tab's controls stayed in the
  connected state (the Disconnect button and port field looked live). Any
  disconnect — clicking Disconnect, an unplug, or a connection error — now
  flows through one path that resets the tab and, for an unexpected drop,
  shows a clear notice. Affects v1.0.0 and v1.1.0.
- **Linux in-app updates now install.** On Linux the updater previously
  downloaded the new build and only opened the file, without applying it.
  When running as an AppImage it now replaces the running image with the
  downloaded one and relaunches, so auto-update works the same as on
  Windows. Other install types (a `.deb` or a source run, which cannot be
  replaced without root) fall back to opening the download for a manual
  install. Affects v1.1.0.
- **Window no longer opens off-screen after a monitor change.** On
  restore, the saved window position is validated against the screens
  currently connected; if it is not visible on any of them (e.g. it was
  last placed on an external monitor that is now disconnected) the
  window is centred on the primary screen. Geometry is also no longer
  saved while minimized.

### Changed

- Manual-mode plot patterns are now evaluated with the `regex` library
  (a new dependency) instead of the standard library `re` module, to get
  the per-line timeout above.

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
