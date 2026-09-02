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
    if not os.path.exists(STATE_FILE):
        return {}

    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, sort_keys=True)


def download_calendar():
    url = os.environ["ICS_URL"]

    # requests does not support webcal:// directly
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/139.0 Safari/537.36"
        ),
        "Accept": "text/calendar,text/plain,*/*",
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    return response.content


def format_datetime(dt):
    if dt is None:
        return ""

    # Convert timezone-aware datetimes to Pacific time
    if dt.tzinfo is not None:
        dt = dt.astimezone(PACIFIC)

    return dt.strftime("%a, %b %-d, %Y at %-I:%M %p PT")


def datetime_to_iso(dt):
    if dt is None:
        return None

    if dt.tzinfo is not None:
        dt = dt.astimezone(PACIFIC)

    return dt.isoformat()


def parse_events(calendar_data):
    calendar = Calendar.from_ical(calendar_data)

    events = {}

    for component in calendar.walk("VEVENT"):
        uid = str(component.get("UID", "")).strip()

        if not uid:
            continue

        summary = str(component.get("SUMMARY", "")).strip()
        location = str(component.get("LOCATION", "")).strip()

        dtstart = component.get("DTSTART")
        dtend = component.get("DTEND")

        start_dt = dtstart.dt if dtstart else None
        end_dt = dtend.dt if dtend else None

        # Skip events that don't have a usable datetime
        if not isinstance(start_dt, datetime):
            continue

        events[uid] = {
            "summary": summary,
            "start": format_datetime(start_dt),
            "end": format_datetime(end_dt),
            "start_iso": datetime_to_iso(start_dt),
            "location": location,
            "fingerprint": hashlib.sha256(
                json.dumps(
                    {
                        "summary": summary,
                        "start_iso": datetime_to_iso(start_dt),
                        "end_iso": datetime_to_iso(end_dt),
                        "location": location,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
        }

    return events


def get_event_datetime(event):
    """
    Safely get an event's start datetime from state.

    Older versions of schedule_state.json may not have start_iso.
    """
    start_iso = event.get("start_iso")

    if not start_iso:
        return None

    try:
        return datetime.fromisoformat(start_iso)
    except (ValueError, TypeError):
        return None


def is_future_event(event):
    dt = get_event_datetime(event)

    if dt is None:
        return False

    now = datetime.now(PACIFIC)

    return dt > now


def diff_events(old_state, new_state):
    added = []
    removed = []

    # New events:
    # Only alert if the new game is in the future.
    for uid, event in new_state.items():
        if uid not in old_state and is_future_event(event):
            added.append(event)

    # Removed events:
    # Only alert if the old game was still in the future.
    #
    # This intentionally ignores old/past games falling off
    # the rolling calendar feed.
    for uid, event in old_state.items():
        if uid not in new_state and is_future_event(event):
            removed.append(event)

    return added, removed


def send_email(subject, body):
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    alert_email = os.environ["ALERT_EMAIL"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = alert_email
    msg.set_content(body)

    with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
        smtp.starttls()
        smtp.login(smtp_user, smtp_password)
        smtp.send_message(msg)


def format_event(event):
    lines = []

    if event.get("summary"):
        lines.append(f"Game: {event['summary']}")

    if event.get("start"):
        lines.append(f"Start: {event['start']}")

    if event.get("end"):
        lines.append(f"End: {event['end']}")

    if event.get("location"):
        lines.append(f"Location: {event['location']}")

    return "\n".join(lines)


def main():
    print("Downloading calendar...")

    calendar_data = download_calendar()
    new_state = parse_events(calendar_data)

    print(f"Current events found: {len(new_state)}")

    old_state = load_state()

    # First run
    if not old_state:
        print("No existing state found.")
        print("Initializing baseline.")

        send_email(
            "Kraken Schedule Watcher Initialized",
            (
                f"The Kraken Hockey League schedule watcher has been initialized.\n\n"
                f"Current events found: {len(new_state)}\n\n"
                f"Future games will trigger email alerts when they are added or removed."
            ),
        )

        save_state(new_state)
        print("Baseline saved.")
        return

    # Upgrade old state files that were created before start_iso existed.
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

    added, removed = diff_events(old_state, new_state)

    # Nothing relevant changed
    if not added and not removed:
        print("No relevant changes found.")

        save_state(new_state)
        return

    # Sort chronologically
    added.sort(key=lambda event: event.get("start_iso") or "")
    removed.sort(key=lambda event: event.get("start_iso") or "")

    subject_parts = []

    if added:
        subject_parts.append(
            f"{len(added)} new game"
            + ("" if len(added) == 1 else "s")
        )

    if removed:
        subject_parts.append(
            f"{len(removed)} future game removed"
            + ("" if len(removed) == 1 else "s")
        )

    subject = "Kraken Schedule: " + ", ".join(subject_parts)

    body_parts = []

    if added:
        body_parts.append("NEW FUTURE GAMES\n")

        for event in added:
            body_parts.append(format_event(event))
            body_parts.append("")

    if removed:
        body_parts.append("REMOVED FUTURE GAMES\n")

        for event in removed:
            body_parts.append(format_event(event))
            body_parts.append("")

    body = "\n".join(body_parts).strip()

    print(subject)
    print(body)

    send_email(subject, body)

    save_state(new_state)

    print("State updated.")


if __name__ == "__main__":
    main()
