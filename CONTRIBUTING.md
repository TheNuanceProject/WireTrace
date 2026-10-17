# Contributing to WireTrace

Thank you for your interest in WireTrace. This page says how to report
a problem, suggest a feature or send a change.

## Who maintains this

WireTrace is built and maintained by one person in spare hours. A reply
to an issue or a pull request can take a week or two. That is normal
and says nothing about your contribution.

## Ways to contribute

### Report a bug

Open an issue with the **Bug report** form. It asks for:

- The version of WireTrace (Help > About WireTrace)
- Your operating system and how WireTrace was installed
- What you did, what you expected and what happened instead
- Steps to reproduce, if you can

WireTrace keeps no log file of its own. Quote the exact words of any
message it showed. On Linux, or when running from source, start it
from a terminal and paste what the terminal printed.

For a security issue, please do not open an issue. See
[SECURITY.md](./SECURITY.md).

### Suggest a feature

Open an issue with the **Feature request** form. Describe:

- The problem you are trying to solve, not only the solution
- How you work around it today
- Who else might benefit

Please discuss a feature in an issue before you start on a pull
request. WireTrace aims to stay focused, and some good ideas will not
fit. A short conversation first saves time on both sides.

### Send a pull request

For small fixes (typos, obvious bugs, documentation), open the pull
request directly. For anything larger, please open an issue first so
the approach can be agreed before the code is written.

Before opening a pull request:

- The code runs with `python main.py`
- The checks below pass
- If you changed what a build contains (a dependency, a resource file,
  the build script), the build completes on your system:
  `python build/build.py` (see [BUILD_GUIDE.md](./BUILD_GUIDE.md))
- No new runtime dependency, unless it was discussed first
- The style matches the surrounding code: PEP 8, type hints on public
  functions, clear module boundaries
- A short description of what you changed and why

## Running the checks

There are two test suites. `tests/` checks the logic against stand-ins
for Qt and needs no display. `tests_gui/` builds the real windows off
screen.

Install once:

    pip install -r requirements-gui-test.txt

Then, from the repository root:

    python -m ruff check .
    python -m pytest
    python -m pytest tests_gui

CI runs the same checks on Linux, Windows and macOS. It also checks
that every source file has its licence header
(`python tools/add_spdx_headers.py --check`) and that the licence file
matches the locked dependencies.

**Merged quickly:**

- Bug fixes with a clear reproduction and a small change
- Documentation improvements
- Refactoring that keeps behaviour and improves clarity

**Discussed first:**

- New features
- New dependencies
- Changes to the serial I/O, threading or logging core
- Changes to the update mechanism or the build

## Scope

WireTrace is a serial monitor. It shows what serial devices send, logs
it, plots it, exports it, and sends commands back.

In scope:

- Improvements to the path from the serial port to the screen and disk
- Better handling of edge cases: disconnects, malformed data, high
  rates
- Export formats that make sense for serial data
- Usability improvements that keep the interface simple
- Fixes for a particular system. Windows and Linux builds are
  published; macOS runs from source.

Out of scope:

- Protocol decoders for particular hardware, which belong in a
  separate tool
- Capturing network data (TCP, UDP)
- Text editing or note-taking
- Anything that adds large dependencies

If you are not sure, ask. "Out of scope" is not a judgement of the
idea; it means the idea belongs in another tool.

## Code of conduct

Be kind and assume good faith. The
[Code of Conduct](./CODE_OF_CONDUCT.md) applies everywhere in the
project.

## Licence

By contributing to WireTrace, you agree that your contributions are
licensed under the
[GNU General Public License, version 3 or later](./LICENSE), the same
as the rest of the project.
