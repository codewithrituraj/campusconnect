import base64
import csv
import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from flask import Flask, Response, g, jsonify, request, send_from_directory
from supabase import create_client

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env", override=True)
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
if not SUPABASE_URL or not SUPABASE_ANON_KEY or not SUPABASE_SERVICE_ROLE_KEY:
    raise RuntimeError("SUPABASE_URL, SUPABASE_ANON_KEY, and SUPABASE_SERVICE_ROLE_KEY are required")

admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
app = Flask(__name__)
# Existing database table name; the extra "r" is part of the real schema.
# Physical schema used by the connected Supabase project.  The API continues
# exposing the original CampusConnect field names for frontend compatibility.
REGISTRATION_TABLE = "registrations"
USER_ID_COLUMN = "uuid"
EVENT_ID_COLUMN = "event_id"
USER_FK_COLUMN = "student_id"
CREATED_AT_COLUMN = "created_at"


def ok(data, status=200, meta=None):
    body = {"ok": True, "data": data}
    if meta is not None:
        body["meta"] = meta
    return jsonify(body), status


def fail(status, code, message):
    return jsonify({"ok": False, "error": {"code": code, "message": message}}), status


def run(query):
    result = query.execute()
    return result.data, getattr(result, "count", None)


def now():
    return datetime.now(timezone.utc)


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def message(exc):
    return str(exc).split(": ", 1)[-1]


def user_shape(row):
    if not row:
        return None
    name = row.get("name") or row.get("email", "").split("@", 1)[0].replace(".", " ").title()
    shaped = {**row, "id": row.get(USER_ID_COLUMN) or row.get("id"), "name": name,
              "student_id": row.get("student_id", ""), "department": row.get("department", ""),
              "class_year": row.get("class_year", ""), "campus": row.get("campus", "")}
    if shaped.get("role") == "organiser":
        shaped["role"] = "organizer"
    return shaped


def upsert_user_profile(user_id, email, name, role="student"):
    """Persist Auth identity in the connected users(uuid,email,role) schema."""
    existing, _ = run(admin.table("users").select(USER_ID_COLUMN).eq(USER_ID_COLUMN, user_id).maybe_single())
    payload = {USER_ID_COLUMN: user_id, "email": email, "role": "organiser" if role == "organizer" else role}
    if existing:
        admin.table("users").update({"email": email}).eq(USER_ID_COLUMN, user_id).execute()
    else:
        admin.table("users").insert(payload).execute()
    row, _ = run(admin.table("users").select("*").eq(USER_ID_COLUMN, user_id).maybe_single())
    return row


def authenticate(required=True):
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else ""
    if not token:
        return fail(401, "UNAUTHORIZED", "Bearer token required") if required else None
    try:
        auth_result = admin.auth.get_user(token)
        auth_user = getattr(auth_result, "user", None)
        if auth_user is None and isinstance(getattr(auth_result, "data", None), dict):
            auth_user = auth_result.data.get("user")
        user_id = getattr(auth_user, "id", None)
        if not user_id:
            raise ValueError("Invalid access token")
        row, _ = run(admin.table("users").select("*").eq(USER_ID_COLUMN, str(user_id)).maybe_single())
        if not row:
            email = getattr(auth_user, "email", None) or (auth_user.get("email") if isinstance(auth_user, dict) else None)
            if email:
                admin.table("users").insert({USER_ID_COLUMN: str(user_id), "email": email, "role": "student"}).execute()
                row, _ = run(admin.table("users").select("*").eq(USER_ID_COLUMN, str(user_id)).maybe_single())
            if not row:
                return fail(401, "PROFILE_MISSING", "User profile not found") if required else None
        g.user = user_shape(row)
        g.user_id = str(user_id)
        return None
    except Exception:
        return fail(401, "UNAUTHORIZED", "Invalid access token") if required else None


def auth(route):
    @wraps(route)
    def wrapped(*args, **kwargs):
        problem = authenticate(True)
        return problem if problem else route(*args, **kwargs)
    return wrapped


def optional_auth(route):
    @wraps(route)
    def wrapped(*args, **kwargs):
        authenticate(False)
        return route(*args, **kwargs)
    return wrapped


def role(*roles):
    def decorator(route):
        @wraps(route)
        @auth
        def wrapped(*args, **kwargs):
            if g.user.get("role") not in roles:
                return fail(403, "FORBIDDEN", "Insufficient role")
            return route(*args, **kwargs)
        return wrapped
    return decorator


def safe(route):
    @wraps(route)
    def wrapped(*args, **kwargs):
        try:
            return route(*args, **kwargs)
        except Exception:
            app.logger.exception("Backend error")
            return fail(500, "DB_ERROR", "Database request failed")
    return wrapped


def event_row(event, count=True):
    event_id = event.get(EVENT_ID_COLUMN) or event.get("id")
    organizer_id = event.get(USER_FK_COLUMN) or event.get("organizer_id")
    starts_at = event.get("timestamp") or event.get(CREATED_AT_COLUMN)
    parsed = parse_time(starts_at) or now()
    filled = 0
    if count:
        registrations, _ = run(admin.table(REGISTRATION_TABLE).select("id").eq(EVENT_ID_COLUMN, event_id))
        filled = len(registrations or [])
    organizer, _ = run(admin.table("users").select("email").eq(USER_ID_COLUMN, organizer_id).maybe_single())
    organizer_name = (organizer or {}).get("email", "").split("@", 1)[0].replace(".", " ").title()
    return {
        **event,
        "id": event_id,
        "organizer_id": organizer_id,
        "starts_at": starts_at,
        "ends_at": iso(parsed + timedelta(hours=1)),
        "status": "published",
        "description": event.get("description") or "",
        "category_slug": event.get("category_slug") or "campus",
        "sub_category": event.get("sub_category") or "Campus event",
        "timezone": "UTC",
        "venue": event.get("venue") or "Campus venue",
        "icon_emoji": event.get("icon_emoji") or "🎓",
        "spots_filled": filled,
        "organizer_name": organizer_name or "Campus organizer",
    }


def all_events():
    rows, _ = run(admin.table("events").select("*").order(CREATED_AT_COLUMN))
    return rows or []


def add_my_status(rows):
    if not getattr(g, "user_id", None) or not rows:
        return rows
    registrations, _ = run(admin.table(REGISTRATION_TABLE).select(EVENT_ID_COLUMN).eq(USER_FK_COLUMN, g.user_id))
    ids = {row["event_id"] for row in (registrations or [])}
    return [{**row, "my_registration_status": "confirmed" if row["id"] in ids else "none"} for row in rows]


@app.after_request
def headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.get("/api/v1/health")
@safe
def health():
    run(admin.table("events").select(EVENT_ID_COLUMN).limit(1))
    run(admin.table("users").select(USER_ID_COLUMN).limit(1))
    run(admin.table(REGISTRATION_TABLE).select("id").limit(1))
    return ok({"status": "ok", "supabase": "ok", "schema": "users-events-registrations"})


@app.get("/api/v1/config")
def config():
    return ok({"supabaseUrl": SUPABASE_URL, "supabaseAnonKey": SUPABASE_ANON_KEY})


@app.get("/api/v1/categories")
def categories():
    return ok([
        {"slug": "campus", "name": "Campus Events", "emoji": "🎓"},
        {"slug": "tech", "name": "Tech & AI", "emoji": "💻"},
        {"slug": "arts", "name": "Arts & Photography", "emoji": "🎨"},
        {"slug": "social", "name": "Social & Fests", "emoji": "🎉"},
        {"slug": "career", "name": "Career & Workshops", "emoji": "📚"},
    ])


@app.get("/api/v1/stats/public")
@safe
def public_stats():
    current = now()
    rows = [event for event in all_events() if (parse_time(event.get(CREATED_AT_COLUMN)) or current) >= current and (parse_time(event.get(CREATED_AT_COLUMN)) or current) < current + timedelta(days=7)]
    clubs, _ = run(admin.table("users").select(USER_ID_COLUMN).in_("role", ["organizer", "organiser"]))
    return ok({"events_this_week": len(rows), "active_clubs": len(clubs or [])})


@app.get("/api/v1/events")
@optional_auth
@safe
def events():
    search = request.args.get("q", "").lower().strip()
    category = request.args.get("category", "all")
    date_filter = request.args.get("date", "upcoming")
    current = now()
    rows = []
    for raw in all_events():
        event = event_row(raw)
        starts = parse_time(event["starts_at"]) or current
        if search and search not in f"{event['title']} {event['venue']} {event['organizer_name']}".lower():
            continue
        if category not in {"", "all"} and event["category_slug"] != category:
            continue
        if date_filter == "upcoming" and starts < current:
            continue
        if date_filter == "today" and (starts.date() != current.date()):
            continue
        if date_filter == "this-week" and starts > current + timedelta(days=7):
            continue
        if date_filter == "this-month" and starts > current + timedelta(days=30):
            continue
        rows.append(event)
    return ok(add_my_status(rows), meta={"page": 1, "limit": len(rows), "total": len(rows)})


@app.get("/api/v1/events/featured")
@optional_auth
@safe
def featured():
    current = now()
    rows = [event_row(raw) for raw in all_events() if (parse_time(raw.get(CREATED_AT_COLUMN)) or current) >= current][:6]
    return ok(add_my_status(rows))


@app.get("/api/v1/events/<event_id>")
@optional_auth
@safe
def event_detail(event_id):
    raw, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, event_id).maybe_single())
    if not raw:
        return fail(404, "NOT_FOUND", "Event not found")
    row = event_row(raw)
    if getattr(g, "user_id", None):
        found, _ = run(admin.table(REGISTRATION_TABLE).select("id").eq(EVENT_ID_COLUMN, event_id).eq(USER_FK_COLUMN, g.user_id).maybe_single())
        row["my_registration_status"] = "confirmed" if found else "none"
    return ok(row)


@app.get("/api/v1/users/me")
@auth
def current_user():
    return ok(g.user)


@app.get("/api/v1/users")
@safe
def find_user():
    email = request.args.get("email", "").strip().lower()
    if not email:
        return fail(400, "VALIDATION_ERROR", "email is required")
    row, _ = run(admin.table("users").select("*").eq("email", email).maybe_single())
    if not row:
        return fail(404, "NOT_FOUND", "User not found")
    return ok(user_shape(row))


@app.post("/api/v1/users")
@auth
@safe
def create_user():
    payload = request.get_json(silent=True) or {}
    email = str(payload.get("email", "")).strip().lower()
    name = str(payload.get("name", "")).strip()
    if not email or "@" not in email or not name:
        return fail(400, "VALIDATION_ERROR", "name and a valid email are required")
    if email != (g.user.get("email") or "").lower():
        return fail(403, "FORBIDDEN", "Profile email does not match the authenticated user")
    # Auth owns credentials and sessions; public.users stores the app profile.
    # Upsert by the authenticated UUID so every login is persisted safely.
    created = upsert_user_profile(g.user_id, email, name, g.user.get("role") or "student")
    if not created:
        return fail(500, "DB_ERROR", "User was not created")
    return ok(user_shape(created), 201)


@app.post("/api/v1/auth/signup-profile")
@safe
def signup_profile():
    payload = request.get_json(silent=True) or {}
    user_id = str(payload.get("user_id", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    try:
        uuid.UUID(user_id)
    except ValueError:
        return fail(400, "VALIDATION_ERROR", "A valid user ID is required")
    if not email or "@" not in email:
        return fail(400, "VALIDATION_ERROR", "A valid email is required")

    auth_response = admin.auth.admin.get_user_by_id(user_id)
    auth_user = getattr(auth_response, "user", None)
    if auth_user is None:
        data = getattr(auth_response, "data", None)
        auth_user = data.get("user") if isinstance(data, dict) else None
    if isinstance(auth_user, dict):
        auth_email = auth_user.get("email")
        metadata = auth_user.get("user_metadata") or {}
    else:
        auth_email = getattr(auth_user, "email", None)
        metadata = getattr(auth_user, "user_metadata", None) or {}
    if not auth_email or auth_email.strip().lower() != email:
        return fail(403, "FORBIDDEN", "Email does not match the Supabase account")

    existing, _ = run(admin.table("users").select("role").eq(USER_ID_COLUMN, user_id).maybe_single())
    name = str(metadata.get("name") or email.split("@", 1)[0]).strip()
    profile = upsert_user_profile(user_id, email, name, (existing or {}).get("role") or "student")
    if not profile:
        return fail(500, "DB_ERROR", "User profile was not created")
    return ok(user_shape(profile), 201)


@app.patch("/api/v1/users/me")
@auth
@safe
def update_user():
    payload = request.get_json(silent=True) or {}
    changes = {key: str(payload[key]).strip() for key in ("name", "student_id", "department", "class_year", "campus") if key in payload}
    if "name" in changes and not changes["name"]:
        return fail(400, "VALIDATION_ERROR", "name cannot be empty")
    if changes:
        supported = {key: value for key, value in changes.items() if key in {"student_id"}}
        if supported:
            admin.table("users").update(supported).eq(USER_ID_COLUMN, g.user_id).execute()
    row, _ = run(admin.table("users").select("*").eq(USER_ID_COLUMN, g.user_id).maybe_single())
    return ok(user_shape(row))


@app.get("/api/v1/users/me/preferences")
@auth
def get_preferences():
    return ok({"user_id": g.user_id, "event_reminders": True, "tech_digest": True})


@app.put("/api/v1/users/me/preferences")
@auth
def put_preferences():
    payload = request.get_json(silent=True) or {}
    return ok({"user_id": g.user_id, "event_reminders": payload.get("event_reminders", True), "tech_digest": payload.get("tech_digest", True)})


@app.post("/api/v1/events/<event_id>/register")
@auth
@safe
def register(event_id):
    event, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, event_id).maybe_single())
    event_start = event.get("timestamp") or event.get(CREATED_AT_COLUMN)
    if not event or (parse_time(event_start) and parse_time(event_start) <= now()):
        return fail(404, "EVENT_UNAVAILABLE", "Event not found or has already started")
    existing, _ = run(admin.table(REGISTRATION_TABLE).select("*").eq(EVENT_ID_COLUMN, event_id).eq(USER_FK_COLUMN, g.user_id).maybe_single())
    if existing:
        return fail(409, "ALREADY_REGISTERED", "Already registered")
    admin.table(REGISTRATION_TABLE).insert({USER_FK_COLUMN: g.user_id, EVENT_ID_COLUMN: event_id, CREATED_AT_COLUMN: iso(now())}).execute()
    row, _ = run(admin.table(REGISTRATION_TABLE).select("*").eq(EVENT_ID_COLUMN, event_id).eq(USER_FK_COLUMN, g.user_id).maybe_single())
    return ok(row, 201)


@app.delete("/api/v1/events/<event_id>/register")
@auth
@safe
def unregister(event_id):
    existing, _ = run(admin.table(REGISTRATION_TABLE).select("id").eq(EVENT_ID_COLUMN, event_id).eq(USER_FK_COLUMN, g.user_id).maybe_single())
    if not existing:
        return fail(404, "REGISTRATION_NOT_FOUND", "Registration not found")
    admin.table(REGISTRATION_TABLE).delete().eq("id", existing["id"]).execute()
    return ok({"deleted": True})


def registration_rows(user_id=None):
    query = admin.table(REGISTRATION_TABLE).select("*")
    if user_id:
        query = query.eq(USER_FK_COLUMN, user_id)
    rows, _ = run(query.order(CREATED_AT_COLUMN, desc=True))
    output = []
    for registration in rows or []:
        event, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, registration[EVENT_ID_COLUMN]).maybe_single())
        if event:
            output.append({**registration, "events": event_row(event), "status": "confirmed"})
    return output


@app.get("/api/v1/registrations/me")
@auth
@safe
def my_registrations():
    return ok(registration_rows(g.user_id))


@app.get("/api/v1/registrations/me/count")
@auth
@safe
def registration_count():
    rows, _ = run(admin.table(REGISTRATION_TABLE).select("id").eq(USER_FK_COLUMN, g.user_id))
    return ok({"count": len(rows or [])})


def pass_code(registration):
    return registration.get("pass_code") or "CC-" + str(registration["id"]).replace("-", "")[:10].upper()


@app.get("/api/v1/registrations/<registration_id>/pass")
@auth
@safe
def registration_pass(registration_id):
    registration, _ = run(admin.table(REGISTRATION_TABLE).select("*").eq("id", registration_id).eq(USER_FK_COLUMN, g.user_id).maybe_single())
    if not registration:
        return fail(404, "NOT_FOUND", "Pass not found")
    event, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, registration[EVENT_ID_COLUMN]).maybe_single())
    if not event:
        return fail(404, "NOT_FOUND", "Event not found")
    code = pass_code(registration)
    image = qrcode.make(json.dumps({"pass_code": code, "registration_id": registration_id, "event_id": event_row(event, count=False)["id"]}))
    stream = io.BytesIO(); image.save(stream, format="PNG")
    payload = {**registration, "pass_code": code, "status": "confirmed", "events": event_row(event), "qr_data_url": "data:image/png;base64," + base64.b64encode(stream.getvalue()).decode()}
    return ok(payload)


def organizer_events():
    rows = all_events()
    if g.user.get("role") not in {"admin"}:
        rows = [row for row in rows if (row.get(USER_FK_COLUMN) or row.get("organizer_id")) == g.user_id]
    return [event_row(row) for row in rows]


@app.get("/api/v1/organizer/stats")
@role("organizer", "admin")
@safe
def organizer_stats():
    events = organizer_events()
    registrations = registration_rows()
    allowed = {event["id"] for event in events}
    count = sum(1 for row in registrations if row["event_id"] in allowed)
    upcoming = [event for event in events if (parse_time(event["starts_at"]) or now()) > now()]
    return ok({"active_events": sum(event["status"] == "published" for event in upcoming), "total_rsvps": count, "registrations_this_week": count, "upcoming_count": len(upcoming)})


@app.get("/api/v1/organizer/events")
@role("organizer", "admin")
@safe
def organizer_event_list():
    return ok(organizer_events())


@app.post("/api/v1/events")
@role("organizer", "admin")
@safe
def create_event():
    payload = request.get_json(silent=True) or {}
    title = str(payload.get("title", "")).strip()
    try:
        capacity = int(payload.get("capacity", 0))
    except (TypeError, ValueError):
        capacity = 0
    starts = payload.get("starts_at") or payload.get("timestamp")
    if not title or capacity <= 0 or not parse_time(starts):
        return fail(400, "VALIDATION_ERROR", "title, positive capacity, and starts_at are required")
    admin.table("events").insert({USER_FK_COLUMN: g.user_id, "title": title, "capacity": capacity, CREATED_AT_COLUMN: starts}).execute()
    row, _ = run(admin.table("events").select("*").eq(USER_FK_COLUMN, g.user_id).eq("title", title).eq(CREATED_AT_COLUMN, starts).maybe_single())
    return ok(event_row(row), 201)


@app.patch("/api/v1/events/<event_id>")
@role("organizer", "admin")
@safe
def update_event(event_id):
    current, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, event_id).maybe_single())
    if not current or (g.user.get("role") != "admin" and current.get(USER_FK_COLUMN) != g.user_id):
        return fail(404, "NOT_FOUND", "Event not found or not owned")
    incoming = request.get_json(silent=True) or {}
    changes = {}
    if "title" in incoming and incoming["title"]:
        changes["title"] = str(incoming["title"]).strip()
    if "capacity" in incoming:
        changes["capacity"] = int(incoming["capacity"])
    if "starts_at" in incoming and parse_time(incoming["starts_at"]):
        changes[CREATED_AT_COLUMN] = incoming["starts_at"]
    if not changes:
        return ok(event_row(current))
    admin.table("events").update(changes).eq(EVENT_ID_COLUMN, event_id).execute()
    row, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, event_id).maybe_single())
    return ok(event_row(row))


@app.post("/api/v1/events/<event_id>/<action>")
@role("organizer", "admin")
@safe
def event_action(event_id, action):
    if action not in {"publish", "cancel"}:
        return fail(404, "NOT_FOUND", "Route not found")
    row, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, event_id).maybe_single())
    if not row or (g.user.get("role") != "admin" and row.get(USER_FK_COLUMN) != g.user_id):
        return fail(404, "NOT_FOUND", "Event not found or not owned")
    return ok(event_row(row))


@app.get("/api/v1/events/<event_id>/attendees")
@role("organizer", "admin")
@safe
def attendees(event_id):
    event, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, event_id).maybe_single())
    if not event or (g.user.get("role") != "admin" and event.get(USER_FK_COLUMN) != g.user_id):
        return fail(404, "NOT_FOUND", "Event not found or not owned")
    rows, _ = run(admin.table(REGISTRATION_TABLE).select("*").eq(EVENT_ID_COLUMN, event_id).order(CREATED_AT_COLUMN))
    output = []
    for registration in rows or []:
        user, _ = run(admin.table("users").select("*").eq(USER_ID_COLUMN, registration[USER_FK_COLUMN]).maybe_single())
        output.append({**registration, "status": "confirmed", "users": user_shape(user)})
    return ok(output)


@app.get("/api/v1/events/<event_id>/attendees/export.csv")
@role("organizer", "admin")
@safe
def export_attendees(event_id):
    response = attendees(event_id)
    if isinstance(response, tuple) and response[1] != 200:
        return response
    data = response[0].get_json()["data"]
    stream = io.StringIO(); writer = csv.writer(stream)
    writer.writerow(["Name", "Email", "Student ID", "Registered At", "Status"])
    for row in data:
        user = row.get("users") or {}
        writer.writerow([user.get("name"), user.get("email"), user.get("student_id"), row.get(CREATED_AT_COLUMN), row.get("status")])
    return Response(stream.getvalue(), mimetype="text/csv")


def ics_time(value):
    parsed = parse_time(value) or now()
    return parsed.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ics_escape(value):
    return str(value or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def calendar_text(events):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//CampusConnect//EN"]
    for event in events:
        start = event.get("timestamp") or event.get("starts_at")
        parsed = parse_time(start) or now()
        lines += ["BEGIN:VEVENT", f"UID:{event['id']}@campusconnect", f"DTSTART:{ics_time(start)}", f"DTEND:{ics_time(iso(parsed + timedelta(hours=1)))}", f"SUMMARY:{ics_escape(event.get('title'))}", "LOCATION:Campus venue", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@app.get("/api/v1/events/<event_id>/calendar.ics")
@safe
def event_calendar(event_id):
    row, _ = run(admin.table("events").select("*").eq(EVENT_ID_COLUMN, event_id).maybe_single())
    if not row:
        return fail(404, "NOT_FOUND", "Event not found")
    return Response(calendar_text([event_row(row, count=False)]), mimetype="text/calendar")


@app.get("/api/v1/registrations/me/calendar.ics")
@auth
@safe
def registration_calendar():
    rows = registration_rows(g.user_id)
    return Response(calendar_text([row["events"] for row in rows]), mimetype="text/calendar")


@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/<path:filename>")
def frontend_file(filename):
    if filename.startswith("api/"):
        return fail(404, "NOT_FOUND", "Route not found")
    if (ROOT / "public" / filename).is_file():
        return send_from_directory(ROOT / "public", filename)
    if (ROOT / filename).is_file():
        return send_from_directory(ROOT, filename)
    return send_from_directory(ROOT, "index.html")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=3000, debug=False)
