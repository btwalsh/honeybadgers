import hashlib
import json
import os
import smtplib
from email.message import EmailMessage

import requests
from icalendar import Calendar

STATE_FILE = "schedule_state.json"


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

    response = requests.get(url, timeout=30)
    response.raise_for_status()

    print("RAW ICS PREVIEW:")
    print(response.text[:1000])  # ADD THIS

    return Calendar.from_ical(response.content)

def download_1calendar():
    url = os.environ["ICS_URL"]

    if url.startswith("webcal://"):
        url = url.replace("webcal://", "https://", 1)

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0 Safari/537.36"
        )
    }

    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

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


def parse_events(calendar):
    events = {}

    for component in calendar.walk():
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID"))

        events[uid] = {
            "summary": str(component.get("SUMMARY", "")),
            "start": str(component.get("DTSTART", "")),
            "end": str(component.get("DTEND", "")),
            "location": str(component.get("LOCATION", "")),
            "fingerprint": event_fingerprint(component),
        }

    return events


def diff_events(old, new):
    added = []
    removed = []
    changed = []

    old_uids = set(old.keys())
    new_uids = set(new.keys())

    for uid in new_uids - old_uids:
        added.append(new[uid])

    for uid in old_uids - new_uids:
        removed.append(old[uid])

    for uid in old_uids & new_uids:
        if old[uid]["fingerprint"] != new[uid]["fingerprint"]:
            changed.append({
                "before": old[uid],
                "after": new[uid]
            })

    return added, removed, changed


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
    return (
        f"Title: {event['summary']}\n"
        f"Start: {event['start']}\n"
        f"End: {event['end']}\n"
        f"Location: {event['location']}\n"
    )


def main():
    old_state = load_state()

    calendar = download_calendar()
    new_state = parse_events(calendar)

    #
    # First run = establish baseline
    #
    if not old_state:
        send_email(
            subject="Kraken Schedule Watcher Initialized",
            body=(
                "Monitoring successfully started.\n\n"
                f"Current events found: {len(new_state)}\n\n"
                "Future additions, removals, and updates "
                "will generate alerts automatically."
            )
        )

        save_state(new_state)
        print("Baseline established.")
        return

    added, removed, changed = diff_events(old_state, new_state)

    if not (added or removed or changed):
        print("No changes found.")
        return

    body_lines = []

    if added:
        body_lines.append("=== NEW GAMES ===\n")

        for event in added:
            body_lines.append(format_event(event))
            body_lines.append("")

    if changed:
        body_lines.append("=== UPDATED GAMES ===\n")

        for change in changed:
            body_lines.append(format_event(change["after"]))
            body_lines.append("")

    if removed:
        body_lines.append("=== REMOVED GAMES ===\n")

        for event in removed:
            body_lines.append(format_event(event))
            body_lines.append("")

    subject_parts = []

    if added:
        subject_parts.append(f"{len(added)} added")

    if changed:
        subject_parts.append(f"{len(changed)} updated")

    if removed:
        subject_parts.append(f"{len(removed)} removed")

    subject = "Kraken Schedule: " + ", ".join(subject_parts)

    send_email(
        subject=subject,
        body="\n".join(body_lines)
    )

    save_state(new_state)

    print("Changes detected and notification sent.")


if __name__ == "__main__":
    main()