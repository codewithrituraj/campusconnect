import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import create_client

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)


def get_config():
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    anon = os.getenv("SUPABASE_ANON_KEY", "").strip()
    service = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    missing = [name for name, value in {"SUPABASE_URL": url, "SUPABASE_ANON_KEY": anon, "SUPABASE_SERVICE_ROLE_KEY": service}.items() if not value]
    if missing:
        raise RuntimeError(f"Missing environment variable(s): {', '.join(missing)}")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.netloc.endswith(".supabase.co"):
        raise RuntimeError("SUPABASE_URL must look like https://your-project-ref.supabase.co")
    return url, anon, service


def check_connection(url, anon):
    request = urllib.request.Request(f"{url}/auth/v1/settings", headers={"apikey": anon, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status < 200 or response.status >= 300:
                raise RuntimeError(f"Supabase Auth returned HTTP {response.status}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Supabase Auth returned HTTP {exc.code}; check the URL and keys") from None
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Supabase: {exc.reason}") from None


def response_data(response):
    """Support Supabase client responses across package versions."""
    if response is None:
        return None
    if isinstance(response, dict):
        return response.get("data", response)
    return getattr(response, "data", None)


def response_user(response):
    if response is None:
        return None
    user = getattr(response, "user", None)
    if user is not None:
        return user
    data = response_data(response)
    if isinstance(data, dict):
        return data.get("user") or data
    return None


def user_field(user, field):
    if isinstance(user, dict):
        return user.get(field)
    return getattr(user, field, None)


def call(label, operation):
    try:
        return operation()
    except Exception as exc:
        raise RuntimeError(f"{label}: {exc}") from None


def execute_seed(url, anon, service):
    check_connection(url, anon)
    admin = create_client(url, service)
    schema_errors = []
    for table in ("users", "events", "regristration"):
        try:
            admin.table(table).select("*").limit(1).execute()
        except Exception as exc:
            schema_errors.append(f"{table}: {exc}")
    if schema_errors:
        raise RuntimeError(
            "Existing database check failed. Verify the table names in the public schema "
            "and that SUPABASE_URL/key point to the same project:\n- " + "\n- ".join(schema_errors)
        )

    organizer_email = os.getenv("CAMPUSCONNECT_SEED_ORGANIZER_EMAIL", "").strip().lower()
    organizer_query = admin.table("users").select("id,email").eq("role", "organizer")
    if organizer_email:
        organizer_query = organizer_query.eq("email", organizer_email)
    organizer = response_data(call("finding an existing organizer profile", lambda: organizer_query.limit(1).maybe_single().execute()))
    if not organizer:
        raise RuntimeError("No existing organizer profile found. Create an account first, set its role to organizer in public.users, then run seed.py.")
    organizer_id = str(organizer["id"])

    events = [
        ("Tech Talk: AI Basics", 150, 1),
        ("Photography Exhibition", 100, 2),
        ("Open Mic Stand Up", 250, 3),
        ("Inter-College Chess Championship", 64, 4),
        ("Career Sprint: Resume & Mock Interviews", 60, 5),
        ("Annual Fall Campus Music Festival", 1000, 6),
    ]
    event_rows = []
    current = datetime.now(timezone.utc)
    for title, capacity, days_ahead in events:
        start = (current + timedelta(days=days_ahead)).replace(hour=16, minute=0, second=0, microsecond=0)
        existing = response_data(call(f"finding event {title}", lambda: admin.table("events").select("id").eq("title", title).maybe_single().execute()))
        payload = {"organizer_id": organizer_id, "title": title, "capacity": capacity, "timestamp": start.isoformat()}
        if existing:
            call(f"updating event {title}", lambda: admin.table("events").update(payload).eq("id", existing["id"]).execute())
            row = response_data(call(f"reading updated event {title}", lambda: admin.table("events").select("*").eq("id", existing["id"]).maybe_single().execute()))
        else:
            call(f"creating event {title}", lambda: admin.table("events").insert(payload).execute())
            row = response_data(call(f"reading created event {title}", lambda: admin.table("events").select("*").eq("title", title).eq("organizer_id", users[0]).maybe_single().execute()))
        if not row or not row.get("id"):
            raise RuntimeError(f"Supabase returned no event row for {title}")
        event_rows.append(row)

    print(f"Seeded {len(event_rows)} events for the existing organizer {organizer.get('email', '')}.")


def add_registration(admin, user_id, event_id):
    existing = response_data(call("checking registration", lambda: admin.table("regristration").select("id").eq("user_id", user_id).eq("event_id", event_id).maybe_single().execute()))
    if not existing:
        call("creating registration", lambda: admin.table("regristration").insert({"user_id": user_id, "event_id": event_id, "createdAt": datetime.now(timezone.utc).isoformat()}).execute())


if __name__ == "__main__":
    try:
        execute_seed(*get_config())
    except Exception as exc:
        print(f"Seed failed: {exc}", file=sys.stderr)
        sys.exit(1)
