# WireTrace Changelog

Notable changes to WireTrace, by version. Versions follow
[Semantic Versioning](https://semver.org/).

---

## [2.0.0] - 2026-10-18

The path from the serial port to the disk has been rebuilt, the
interface redesigned, updates are verified by signature, and the
licence has changed.

### Licence

- **WireTrace is now under the GNU General Public License, version 3
  or later.** WireTrace stays free to use, study, change and share;
  a version that is shared stays open source under the same licence.
  Versions 1.0.0 to 1.2.0 remain under the MIT License.

### Changed: data

- **Reading, writing and drawing are separate.** The port is read on
  its own thread and the log is written on another. A busy window or a
  slow disk does not delay reading, and the log does not pass through
  the window on its way to disk.
- **Disconnect keeps the last lines.** On disconnect, an unplugged
  device, or closing a tab or the application, the port is closed
  first, the final partial line is taken, and everything waiting is
  written and synced before the result is reported.
- **Export saves the session, not the screen.** Every session is
  recorded to a temporary file from the moment of connection, up to
  1 GB a tab, so Export works without a log running and includes lines
  the console no longer holds. Clear sets where the next export
  starts. The export runs in the background, and a session can still
  be exported after its device has disconnected. The recording is
  removed when the tab closes or connects again.
- **One timestamp per line.** A line is stamped once, when it is read,
  and the console, the log, the CSV and the plot all use that stamp.
- **CSV rows are chosen by content.** A line becomes a row because it
  carries the detected columns, not because of its severity tag.
  Detection retries as data arrives, so a boot log before the readings
  does not force raw mode, provided the readings begin within the
  first 200 lines. Negative numbers are written as numbers.
- **A log always has its text file.** The choice when starting a log
  is Text, or Text and CSV; a CSV is not written alone.
- **Blank lines are kept.** A preference hides them in the console
  only.
- **Console memory is bounded.** The console holds the most recent
  lines up to its limit, counted in lines and in characters. When a
  device outpaces the display, the console skips ahead and says how
  many lines it skipped; they are still recorded and logged.
- **HEX mode rebuilt.** Bytes are shown and recorded as `hexdump -C`
  lays them out: the offset, sixteen bytes in two groups of eight, and
  the printable characters between bars. On screen the offset, the bars
  and the dots for unprintable bytes are drawn quieter than the bytes.
- **Log files end lines with LF on every system.** On Windows they
  ended with CR LF. A preference restores CR LF.

### Changed: interface

- **The interface has been redesigned.** A new tab opens on a list of
  serial ports that updates by itself. Each tab has its own toolbar,
  level buttons with counts, command row and status bar.
- **Menus are arranged by task:** File, Edit, View, Device, Help.
  Themes moved to View > Theme.
- **Keyboard shortcuts changed.** Ctrl+S opens Export session; it was
  Quick Save. Ctrl+E is not used. The live plot is Ctrl+P; it was
  Ctrl+Shift+P. New: Ctrl+Shift+C copies without timestamps, Ctrl+A
  selects all, F5 refreshes the ports, F2 renames the tab, and Ctrl+1
  to Ctrl+9 fetch saved commands. Help > Keyboard shortcuts lists
  them all.
- **Themes are Light, Dark and System.** System follows your computer
  and is the default. On Windows, the title bar of every window follows
  the theme chosen, at once.
- **One design with thenuanceproject.com.** WireTrace follows the
  Nuance Design Language: its colours, sizes and type come from the
  same design tokens as the website. Interface text is Poppins, which
  comes with WireTrace, at one scale of sizes in pixels, so the
  interface looks the same on Windows, Linux and macOS and follows the
  display's scaling; menus, lists and tooltips use it too, and every
  system draws its letters as the font draws them. The WireTrace mark
  is drawn in the Nuance accent. Outlines of fields and buttons are
  firmer (3:1), meta text and links meet 4.5:1, the keyboard focus is a
  visible ring, and the icons are Lucide's.
- **A window is never smaller than its content needs,** so no text is
  cut off at any display scale.
- **The console font is JetBrains Mono, which comes with WireTrace,**
  with its joined symbols switched off, so a pair such as `->` or `!=`
  shows as the two characters the device sent. Its size is in pixels,
  from 12 to 40; it starts at 13.
- **On the first start after updating from 1.x, the theme is set to
  System and the console font and its size to the 2.0 defaults, once.**
  Every other setting is kept.
- **Messages work one way.** A short message says that something
  finished, a bar at the top of the tab reports a problem until it is
  over, and a dialog appears only when a decision is needed.
- **After a lost connection the tab keeps its lines** and offers to
  connect again, export the session and open the log folder.
- **Preferences are arranged in pages** and have Restore defaults.
  New: log folder, maximum log file size, line endings in files, hide blank
  lines.
- **The plot has a list of values with latest, least, greatest and
  average,** in place of the legend.
- **Point at the plot to read it.** A line marks the nearest sample, a
  dot each value, and a card gives the time it was received and each
  value then.
- **The plot has a close button** at the right end of its toolbar.
- **The user guide has been rewritten** and is filled in from the
  program, so its shortcuts, limits and folders are those of the
  version you run.

### Changed: updates

- **Updates are verified by signature.** A new version is accepted
  only if its description carries a valid signature, and a download
  is used only if it is exactly the file
  that description names. The download address is built by WireTrace
  itself.
- **Checks continue while WireTrace stays open**, every 12 hours.
  A new version is announced by a message and never interrupts a
  session. Skip and Remind later are remembered across restarts.
- **An update check states one more thing:** the day of the previous
  check, as the website named it. It is the same for everyone who
  checked that day, so installations can be counted without
  identifying anyone. As before, a check states the WireTrace version,
  the operating system, the processor type and how WireTrace was
  installed. See the privacy policy.
- **A `.deb` or source install is sent to the release page** instead
  of being offered a download it cannot install.
- **Downloaded updates are kept in WireTrace's own cache folder,** not
  the system's temporary folder.
- **The Windows installer** shows the privacy statement, offers the
  update check as an option, and on uninstalling removes working files
  and leaves settings and logs.

### Added

- **Usage statistics, if you choose to share them.** Off unless
  switched on. WireTrace asks once, with a bar that blocks nothing, and
  the setting is in Preferences, under Updates and privacy. Switching
  them on makes a random installation identifier. A report for each day
  of use says which features were used, whether lines were lost, a log
  could not be written, a connection was lost or WireTrace closed
  unexpectedly, and, as ranges, the busiest line rate, the connections,
  the longest time a window stayed open and the starts; never anything
  a device sent. Notes are kept on disk as they happen, so a sudden
  close loses nothing, several open windows share them safely, and
  reports are never sent while WireTrace closes. Switching statistics
  off deletes everything kept on the computer and asks the server to
  delete every report sent. The privacy policy lists every value.
- **Several WireTrace windows can be open at once** without one losing
  what another saved: preferences are written under a lock, after
  reading what the others wrote.
- **Maximum log file size.** A log that reaches the size set in
  Preferences (100 MB by default; Off keeps one file) continues in a
  new file, numbered `_002`, `_003` and so on, so long captures stay
  easy to open. A line is never cut, each file has its own header, and
  while the limit is on a CSV file also ends at 1,000,000 rows.
- **Firmware log levels.** ESP-IDF (`E (123) tag:`), Zephyr (`<err>`)
  and bracketed (`[ERROR]`) level markers decide a line's severity
  before the keyword search.
- **Loss is never silent.** If the disk cannot keep up or fails, the
  number of lines not written is reported and marked in the file at the
  place they would have been.
- **Saved commands.** Up to 20, the first nine on Ctrl+1 to Ctrl+9.
- **Line ending for commands:** None, LF, CR or CR LF.
- **The log folder is remembered.** A log is offered the folder the
  previous log was started in, in every tab.
- **Each port remembers** its baud rate, line ending, plot profile and
  tab name.
- **Tabs can be renamed** (F2 or double-click).
- **Filter by level** with one click, with a count on each level.
- **Copy without timestamps, and Export selection,** from the console
  menu.
- **Plot: Measure,** two cursors that stop on samples and hold the
  plot still, with a table of A, B and B minus A: the time between them
  and the change in each value. **Panels by scale**, and **Pop out**
  into a window of its own.
- **Help > Report a problem** and **About > Third-party licences**.

### Removed

- **Quick Save.** Export session (Ctrl+S) takes its place.
- **CSV-only logs.** A log always has its text file.
- **The macOS build and the `--platform` option of `build/build.py`.**
  A build is made on the system it is for; macOS runs from source.

### Fixed

- Existing log files are never overwritten; a clashing name gets a
  numbered suffix.
- Bytes that are not valid UTF-8 are shown as `\xNN` rather than
  replaced.
- Leading and trailing spaces of a line are kept, so indented output
  reads as the device sent it.
- Preferences containing a `%` character load correctly.
- A very long line with no separators does not stall CSV logging or
  freeze the plot.
- Cursor, private-mode and window-title escape sequences are removed
  like colour codes, instead of appearing as text.
- A CSV column name containing a comma or starting with `=` is escaped
  like a value, and a key repeated on one line gives one column.
- Search results stay correct after the console is drawn again or
  cleared.
- With a filter active, the console does not show lines it has
  already let go of.

### Security

- Text that comes from a device, a port's description or the update
  server is always shown as plain text, never read as markup.
- Control characters, Unicode line separators and direction overrides
  in received data are written as escapes in the console and in logs.
- Memory stays bounded when a device sends very long lines or a
  stream without line breaks.
- A plot pattern that keeps running out of time stops plotting and
  says so, instead of holding up the window.
- The download is checked against the signed description again at the
  moment it is installed.
- An AppImage is replaced only when WireTrace really runs from it.
- The recording of a session, and the settings file, are readable by
  their owner only.
- A damaged settings file cannot stop WireTrace from starting.

### Building and development

- **Releases are built by a workflow**, with Python 3.12, from
  dependencies pinned by hash, and come with checksums, a bill of
  materials, a build provenance attestation and the licence texts of
  everything they contain.
- **Linux builds run on older systems.** They are built on Ubuntu
  22.04 and refuse to need a C library newer than glibc 2.35.
- New runtime dependency: `cryptography`, to verify signatures.
- A second test suite, `tests_gui/`, runs the real interface on Linux,
  Windows and macOS.
- Added a Code of Conduct, issue forms and private vulnerability
  reporting.
- The repository shows a Sponsor button, for GitHub Sponsors, and
  Help > About has a Support button that opens the same page.
  Sponsoring is optional; WireTrace never asks for it on its own.

---

## [1.2.0] - 2026-06-21

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
  rather than logged as noise. Stripping only: terminal rendering
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
  the complete application into the AppImage (or, if the AppImage tool
  is unavailable, a tarball of the full directory), so the artifact runs
  on its own. Affects v1.1.0.
- **The tab now updates when a device drops on its own.** When a device
  was unplugged or the connection was lost, the port was released and the
  status bar said "Disconnected," but the tab's controls stayed in the
  connected state (the Disconnect button and port field looked live). Any
  disconnect (clicking Disconnect, an unplug, or a connection error) now
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

## [1.1.0] - 2026-05-29

First feature release after v1.0.0. Adds the live data plotter,
manual plot configuration with named profiles, two data-protection
fixes, and parser improvements. Fully backwards-compatible: no API
breaks, no file-format breaks, no user action required to upgrade.

### Added: Live data plotter

- **Docked plot panel** beneath the console. Auto-detects three
  structured formats: JSON object lines, `key:value` pairs,
  positional values with auto-detected delimiter (comma, tab,
  semicolon, pipe, whitespace).
- **`plt:` / `plt0:` / `plt1:` prefix routing** for selectively
  plotting only tagged lines, and `plt!Name1,Name2,…` for explicit
  column naming.
- **Frequency-based detection**: a column must appear in ≥70% of
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

### Added: Manual plot configuration

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
  (pattern valid but no current matches: firmware may simply be
  quiet), Red (structurally broken). Apply is enabled for Green or
  Amber, disabled for Red. No "broken" UX state.
- **Named profiles**: save, rename, delete, set as default.
  Profiles persist in `preferences.ini` and survive app restarts.
  Built-in **Auto-detect** profile is protected from rename/delete.
- **Default profile** is applied automatically on new tabs and on
  reconnection so engineers don't have to re-enter their config
  every session.

### Changed: Data protection

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

### Changed: Tag detector & CSV

- Tag detector uses word-boundary regex per severity to eliminate
  false positives like *"default"* matching *"fault"*.
- CSV auto-detection samples only DATA-tagged rows; severity and
  command lines no longer pollute the schema and are routed only
  to the .txt log.

### Fixed

- **Status bar metrics reset on disconnect**: port, baud, data rate,
  and line count no longer show stale values from the previous
  session. This affected v1.0.0 too; the bar now clears cleanly when
  a device disconnects, regardless of how the disconnect happened
  (user-initiated, cable pull, or remote close).

### Discoverability

- Tooltips added to hidden plot affordances: pan/zoom on plot main
  area and per-axis, right-click context menu, legend click-to-
  toggle (each legend entry's coloured sample toggles that trace's
  visibility), splitter handle resize, and the Window dropdown's
  view-filter semantics. No popups, no first-run tour: quiet
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

## [1.0.0] - 2026-04-15

Initial public release of WireTrace. Multi-device serial monitor
with buffered logging, structured CSV export, search and live
filter, severity classification, and the Studio Light / Midnight
Dark themes. Cross-platform; pre-built Windows installer published
on GitHub Releases.
