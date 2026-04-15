# Security Policy

## Reporting a vulnerability

If you find a security issue in WireTrace, please **do not open a
public issue**. Send the details by email to:

**shahin@thenuanceproject.com**

Include:

- The version of WireTrace affected
- A description of the issue and its potential impact
- Steps to reproduce, if possible
- Whether you'd like to be credited in the fix announcement

## What to expect

WireTrace is maintained by a single person, so response times are
measured in days, not hours. You can expect:

- **Acknowledgement within 14 days** of your report
- An assessment of severity and a target fix timeline shortly after
- A fix released as a new version, with notes in the release page
- Credit in the release notes if you'd like (or anonymity if you
  prefer)

## Scope

WireTrace reads from serial ports and writes to the local
filesystem. The security surface is small but not zero. Issues
that would be taken seriously include:

- Arbitrary code execution triggered by malformed serial data
- Arbitrary file writes outside the configured log directory
- Tampering with the auto-update mechanism
- Unauthorized access to the update channel

Issues that are **not** security issues:

- A hardware device misbehaving because of data you sent it
  (that's a device issue, not a WireTrace issue)
- Performance degradation under unrealistic load
- Feature requests framed as security concerns

## Supported versions

Only the most recent release receives security fixes. Older
versions will not be patched.

Thanks for helping keep WireTrace safe.
