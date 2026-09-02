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
    rescheduled = []

    # New future games
    for uid, event in new_state.items():
        if uid not in old_state and is_future_event(event):
            added.append(event)

    # Removed future games
    for uid, event in old_state.items():
        if uid not in new_state and is_future_event(event):
            removed.append(event)

    # Future games whose scheduled time changed
    for uid, old_event in old_state.items():
        if uid not in new_state:
            continue

        new_event = new_state[uid]

        # Only care about games that were still in the future
        # according to the old schedule.
        if not is_future_event(old_event):
            continue

        old_start = old_event.get("start_iso")
        new_start = new_event.get("start_iso")

        old_end = old_event.get("end_iso")
        new_end = new_event.get("end_iso")

        if old_start != new_start or old_end != new_end:
            rescheduled.append(
                {
                    "old": old_event,
                    "new": new_event,
                }
            )

    return added, removed, rescheduled


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


def format_reschedule(reschedule):
    old_event = reschedule["old"]
    new_event = reschedule["new"]

    lines = []

    if new_event.get("summary"):
        lines.append(f"Game: {new_event['summary']}")

    lines.append("")
    lines.append(f"OLD TIME: {old_event.get('start', '')}")

    if old_event.get("end"):
        lines.append(f"OLD END:  {old_event.get('end', '')}")

    lines.append("")
    lines.append(f"NEW TIME: {new_event.get('start', '')}")

    if new_event.get("end"):
        lines.append(f"NEW END:  {new_event.get('end', '')}")

    if new_event.get("location"):
        lines.append(f"Location: {new_event['location']}")

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
                f"Future games will trigger email alerts when they are "
                f"added, removed, or rescheduled."
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

    added, removed, rescheduled = diff_events(old_state, new_state)

    # Nothing relevant changed
    if not added and not removed and not rescheduled:
        print("No relevant changes found.")

        save_state(new_state)
        return

    # Sort chronologically
    added.sort(key=lambda event: event.get("start_iso") or "")
    removed.sort(key=lambda event: event.get("start_iso") or "")
    rescheduled.sort(
        key=lambda item: item["new"].get("start_iso") or ""
    )

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

    if rescheduled:
        subject_parts.append(
            f"{len(rescheduled)} game rescheduled"
            + ("" if len(rescheduled) == 1 else "s")
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

    if rescheduled:
        body_parts.append("RESCHEDULED FUTURE GAMES\n")

        for reschedule in rescheduled:
            body_parts.append(format_reschedule(reschedule))
            body_parts.append("")

    body = "\n".join(body_parts).strip()

    print(subject)
    print(body)

    send_email(subject, body)

    save_state(new_state)

    print("State updated.")


if __name__ == "__main__":
    main()
```
