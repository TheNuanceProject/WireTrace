# SPDX-License-Identifier: GPL-3.0-or-later
"""The privacy statement: what WireTrace sends, and when.

It is shown by the Windows installer before anything is installed, and
in the application, as the statement itself and not a summary of it. It
is written here, beside the code that makes the update check
(``updater/update_manager.py``) and sends usage statistics
(``updater/usage.py``), so that a change to what is sent and a change to
what is said about it are made in one place. Tests hold the statement to
the fields the code sends. The website's privacy policy says the same at
greater length, with the rights of users and how to use them.

The installer shows it as plain text, so it is plain ASCII.

This module has no dependency beyond the constants.
"""

from __future__ import annotations

from app.constants import (
    UPDATE_CHECK_INTERVAL_HOURS,
    USAGE_DELETION_COMPLETE_DAYS,
    USAGE_RECORDS_KEPT_MONTHS,
    USAGE_REPORT_MAX_AGE_DAYS,
)
from version import (
    APP_AUTHOR,
    APP_DEVELOPER,
    APP_MAINTAINER_EMAIL,
    APP_NAME,
    APP_PRIVACY_URL,
    APP_WEBSITE,
)

_WEBSITE_NAME = APP_WEBSITE.removeprefix("https://")

PRIVACY_STATEMENT = (
    f"{APP_NAME} Privacy Statement\n"
    "\n"
    f"{APP_NAME} is published by {APP_DEVELOPER} as {APP_AUTHOR} "
    f"({_WEBSITE_NAME}), who is responsible for the information described "
    f"here. This statement describes everything {APP_NAME} sends over the "
    "network. The full privacy policy, which also sets out your rights and "
    f"how to exercise them, is at {APP_PRIVACY_URL}\n"
    "\n"
    "Your data stays on your computer\n"
    "\n"
    f"What your devices send is shown by {APP_NAME} and written only to the "
    f"files you choose. {APP_NAME} never sends it anywhere.\n"
    "\n"
    "1. The update check\n"
    "\n"
    f"While the update check is on, {APP_NAME} asks {_WEBSITE_NAME} for the "
    f"latest version when it starts and every {UPDATE_CHECK_INTERVAL_HOURS} "
    "hours while it stays open. Each request states the version of "
    f"{APP_NAME}, the operating system, the processor type, how {APP_NAME} "
    "was installed and the date of the previous check. It carries no "
    "identifier and nothing about you, your devices or your data.\n"
    "\n"
    "Like any request over the internet, it reaches the server from a "
    "network address. The server derives from that address the country and "
    "an approximate area of about one degree of latitude and longitude "
    "(roughly 100 km). It uses the address itself for no more than one "
    "minute, to limit abuse, and never stores it. Only daily totals are "
    "kept; no record of a single request is kept. The answer itself, the "
    "description of the latest version, is a file on the website, which "
    "GitHub hosts: the request is passed to GitHub to fetch it (see 3).\n"
    "\n"
    "You can turn the update check off on a later page of this installer, "
    "and at any time in Preferences.\n"
    "\n"
    "2. Usage statistics\n"
    "\n"
    "Usage statistics are off, and this installer does not switch them on. "
    f"{APP_NAME} asks you once whether you want to share them, and you can "
    "change your answer at any time in Preferences.\n"
    "\n"
    f"If you switch them on, {APP_NAME} creates a random installation "
    "identifier on your computer. It is not derived from your computer, "
    f"your name or your network. For each day on which {APP_NAME} is used, "
    "it then sends one report, which states: a random installation "
    "identifier; the date of the day it describes; the version of "
    f"{APP_NAME}; the operating system; the processor type; how "
    f"{APP_NAME} was installed; which features were used; whether lines "
    "were lost, a log could not be written, a connection was lost or "
    f"{APP_NAME} closed unexpectedly; the highest line rate and the number "
    "of connections opened, each as a range; the longest time one window "
    f"stayed open, as a range; how many times {APP_NAME} was started, as a "
    "range; the theme; and the display scale. A report never contains a "
    "port or device name, a file name, a command, or anything a device "
    "sent. A report that cannot be sent is tried again for up to "
    f"{USAGE_REPORT_MAX_AGE_DAYS} days and then discarded.\n"
    "\n"
    "The server stores each report under a one-way keyed hash of the "
    "identifier, never the identifier itself, and never the network "
    f"address. Each report is kept for {USAGE_RECORDS_KEPT_MONTHS} months "
    "from the day it describes. After that only totals remain, which do not "
    "identify any installation.\n"
    "\n"
    f"If you switch usage statistics off, {APP_NAME} deletes the "
    "identifier and everything it was keeping for usage statistics on your "
    "computer, and asks the server to delete every report sent under that "
    "identifier. The server deletes them as soon as the request arrives, "
    f"and they are gone from every copy within {USAGE_DELETION_COMPLETE_DAYS} days.\n"
    "\n"
    "3. Who handles the information\n"
    "\n"
    f"The server at {_WEBSITE_NAME} runs on Cloudflare, which processes the "
    f"requests on behalf of {APP_AUTHOR} and may do so in any country where "
    "it operates. The website, including the file the update check reads, "
    "is hosted on GitHub Pages: GitHub receives each update check with its "
    "network address, as it does every visit to the website, and keeps it "
    "in its own security logs. Usage reports and deletion requests never "
    "reach GitHub. Totals that identify no installation, such as the number "
    "of active installations and the countries they are in, are published "
    f"on {_WEBSITE_NAME}. Nothing else is published, sold, shared or used "
    "for advertising.\n"
    "\n"
    f"The full privacy policy: {APP_PRIVACY_URL}\n"
    f"Questions and requests: {APP_MAINTAINER_EMAIL}\n"
)
