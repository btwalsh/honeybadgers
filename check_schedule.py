import hashlib
import json
import os
import smtplib
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

import requests
from icalendar import Calendar


STATE_FILE = "schedule_state.json"
PACIFIC = ZoneInfo("America/Los_Angeles")


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)

    return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def download_calendar():
    url = os.environ["ICS_URL"]

    if url.startswith("webcal://"):
        url = url.replace("webcal://", "https://", 1)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        ),
        "Accept": "text/calendar,text/plain,*/*",
    }

    response = requests.get(
        url,
        headers=headers,
        timeout=30
    )

    response.raise_for_status()

    print("RAW ICS PREVIEW:")
    print(response.text[:2000])

    return Calendar.from_ical(response.content)


def event_fingerprint(event):
    uid = str(event.get("UID", ""))

    start = str(event.get("DTSTART", ""))
    end = str(event.get("DTEND", ""))
    summary = str(event.get("SUMMARY", ""))
    location = str(event.get("LOCATION", ""))

    raw = "|".join([
        uid,
        start,
        end,
        summary,
        location,
    ])

    return hashlib.sha256(raw.encode()).hexdigest()


def format_datetime(dt_field):
    if not dt_field:
        return ""

    dt = dt_field.dt

    # Handle date-only events
    if not hasattr(dt, "astimezone"):
        return str(dt)

    dt = dt.astimezone(PACIFIC)

    return dt.strftime("%a %b %d, %Y %I:%M %p %Z")


def datetime_to_iso(dt_field):
    if not dt_field:
        return ""

    dt = dt_field.dt

    # Handle date-only events
    if not hasattr(dt, "astimezone"):
        return datetime(
            dt.year,
            dt.month,
            dt.day,
            tzinfo=PACIFIC
        ).isoformat()

    return dt.astimezone(PACIFIC).isoformat()


def parse_events(calendar):
    events = {}

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID"))

        events[uid] = {
            "summary": str(component.get("SUMMARY", "")),
            "start": format_datetime(component.get("DTSTART")),
            "end": format_datetime(component.get("DTEND")),
            "start_iso": datetime_to_iso(component.get("DTSTART")),
            "location": str(component.get("LOCATION", "")),
            "fingerprint": event_fingerprint(component),
        }

    return events


def get_event_datetime(event):
    """
    Return the event's start time as a timezone-aware datetime.

    Handles both the current state format and the older state format.
    """
    start_iso = event.get("start_iso")

    if start_iso:
        return datetime.fromisoformat(start_iso)

    # Old state files did not contain start_iso.
    # We cannot reliably reconstruct the original datetime from
    # the formatted display string, so treat old events as unknown.
    return None


def is_future_event(event):
    start = get_event_datetime(event)

    if start is None:
        return False

    return start > datetime.now(PACIFIC)


def diff_events(old, new):
    added = []
    removed = []

    old_uids = set(old.keys())
    new_uids = set(new.keys())

    # New events
    for uid in new_uids - old_uids:
        if is_future_event(new[uid]):
            added.append(new[uid])

    # Removed events
    for uid in old_uids - new_uids:
        if is_future_event(old[uid]):
            removed.append(old[uid])

    # We intentionally DO NOT report changed events.
    #
    # Completed games frequently get updated with scores or other
    # information. Those changes are not useful schedule alerts.
    #
    # If we later want to notify about future-game reschedules,
    # that can be added separately.

    return added, removed


def send_email(subject, body):
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    recipient = os.environ["ALERT_EMAIL"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = recipient
    msg.set_content(body)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(user, password)
        smtp.send_message(msg)


def format_event(event):
    lines = [
        event["summary"],
        f"Start: {event['start']}",
    ]

    if event.get("location"):
        lines.append(f"Location: {event['location']}")

    return "\n".join(lines)


def main():
    old_state = load_state()

    calendar = download_calendar()
    new_state = parse_events(calendar)

    print(f"Events currently found: {len(new_state)}")

    #
    # First run / empty baseline
    #
    if not old_state:
        send_email(
            subject="Kraken Schedule Watcher Initialized",
            body=(
                "Monitoring successfully started.\n\n"
                f"Current events found: {len(new_state)}\n\n"
                "Future additions and removals will "
                "generate alerts automatically."
            )
        )

        save_state(new_state)

        print("Baseline established.")
        return

    #
    # Detect whether this is an old-format state file.
    #
    old_state_needs_upgrade = any(
        "start_iso" not in event
        for event in old_state.values()
    )

    if old_state_needs_upgrade:
        print("Old state format detected.")
        print("Rebuilding baseline with current format.")

        save_state(new_state)

        print("State file upgraded.")
        return

    #
    # Normal diff
    #
    added, removed = diff_events(old_state, new_state)

    print(f"New future games: {len(added)}")
    print(f"Removed future games: {len(removed)}")

    if not added and not removed:
        print("No relevant changes found.")

        # Always save the latest state so score/metadata changes
        # don't repeatedly affect future comparisons.
        save_state(new_state)

        return

    body_lines = []

    if added:
        body_lines.append("=== NEW GAMES ===")
        body_lines.append("")

        for event in sorted(added, key=lambda e: e["start_iso"]):
            body_lines.append(format_event(event))
            body_lines.append("")

    if removed:
        body_lines.append("=== REMOVED FUTURE GAMES ===")
        body_lines.append("")

        for event in sorted(removed, key=lambda e: e["start_iso"]):
            body_lines.append(format_event(event))
            body_lines.append("")

    subject_parts = []

    if added:
        subject_parts.append(
            f"{len(added)} new game"
            + ("" if len(added) == 1 else "s")
        )

    if removed:
        subject_parts.append(
            f"{len(removed)} future game"
            + ("" if len(removed) == 1 else "s")
            + " removed"
        )

    subject = "Kraken Schedule: " + ", ".join(subject_parts)

    send_email(
        subject=subject,
        body="\n".join(body_lines)
    )

    save_state(new_state)

    print("Notification sent.")
    print("State updated.")


if __name__ == "__main__":
    main()
