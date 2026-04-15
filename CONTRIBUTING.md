# Contributing to WireTrace

Thanks for taking the time to look at this file. A few things to
know up front, then the specifics.

## Who maintains this

WireTrace is built and maintained by one person in spare hours.
That shapes everything below. If it takes me a week or two to
respond to an issue or pull request, that's normal, not a signal
that you did something wrong.

## Ways to contribute

### Report a bug

Open an issue with:

- What version of WireTrace you're running (Help → About)
- Your operating system and version
- What you did, what you expected, what happened instead
- Steps to reproduce, if you can
- A log snippet or screenshot if relevant

If WireTrace crashed, check the logs directory for any crash logs
and attach them:

- **Windows:** `%APPDATA%\WireTrace\Logs\`
- **macOS:** `~/Library/Application Support/WireTrace/Logs/`
- **Linux:** `~/.config/WireTrace/Logs/`

### Suggest a feature

Open an issue with the **feature request** label. Describe:

- The problem you're trying to solve (not just the solution)
- How you're working around it today
- Who else might benefit

Please open an issue to discuss before starting work on a feature
PR. WireTrace aims to stay focused, and some otherwise-good
features won't fit the scope. A short conversation saves everyone
time.

### Send a pull request

For small fixes (typos, obvious bugs, documentation), just open
the PR. For anything larger, please open an issue first so we can
agree on the approach before you write the code.

**Before opening a PR:**

- The code runs locally with `python main.py`
- The build completes for your platform:
  - Windows: `python build/build.py --platform windows --version 1.0.0`
  - macOS: `python build/build.py --platform macos --version 1.0.0`
  - Linux: `python build/build.py --platform linux --version 1.0.0`
- No new runtime dependencies unless we've discussed it
- Code style matches the surrounding file (PEP 8, type hints on
  public functions, clear module boundaries)
- A short description of what you changed and why

**What gets merged quickly:**

- Bug fixes with a clear reproduction and a minimal diff
- Documentation improvements
- Refactors that preserve behaviour and improve clarity

**What gets discussed first:**

- New features
- New dependencies
- Changes to the serial I/O, threading, or logging core
- Changes to the update mechanism or build pipeline

## Scope — what belongs and what doesn't

WireTrace is a serial monitor. It shows serial data, logs it to
disk, and exports it. That's it.

In scope:

- Improvements to the serial I/O path
- Better handling of edge cases (disconnects, malformed data, large
  throughput)
- New export formats that make sense for serial data
- Visual and usability improvements that don't bloat the UI
- Cross-platform fixes (macOS and Linux — the build scripts support
  both, but pre-built binaries aren't published yet)

Out of scope:

- Protocol decoders for specific hardware (too specialized;
  belongs in a plugin or a separate tool)
- Network/TCP/UDP data capture (different problem)
- Rich text editing or note-taking features
- Anything that adds significant dependencies

If you're not sure, ask. "Out of scope" is not a rejection of the
idea — it just means it doesn't belong in this specific tool.

## Code of conduct

Be kind. Assume good faith. Criticize code, not people. If
something feels off about an interaction, say so calmly and we'll
sort it out.

## License

By contributing to WireTrace, you agree that your contributions
will be licensed under the [MIT License](./LICENSE), same as the
rest of the project.
