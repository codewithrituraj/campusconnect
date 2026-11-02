# CampusConnect

CampusConnect is a plain HTML/CSS/JS frontend backed by a Python Flask API, Supabase Auth, and Supabase Postgres. The frontend is unchanged; Flask preserves the `/api/v1` contract used by `api.js` and `app.js`. The backend uses the existing `public.users`, `public.events`, and misspelled `public.regristration` table.

## Setup

1. Create a Supabase project.
2. Confirm the existing `users`, `events`, and `regristration` tables match the diagram. No new tables or migration are required.
3. Copy `.env.example` to `.env` and fill the three values from Supabase **Project Settings → API**:

   ```env
   SUPABASE_SERVICE_ROLE_KEY=
   SUPABASE_ANON_KEY=
   SUPABASE_URL=https://your-project-ref.supabase.co
   ```

4. Enable the **Email** provider under Supabase **Authentication → Providers**.
5. Install Python packages. After creating your first account in the app, set that profile's `role` to `organizer` in `public.users`, then optionally seed events:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   # Optional: use the organizer matching this email
   CAMPUSCONNECT_SEED_ORGANIZER_EMAIL=you@university.edu python seed.py
   ```

6. Start the backend and open `http://localhost:3000`:

   ```bash
   python app.py
   ```

There is no built-in or demo account. Create accounts from the **Create an account** form. Never expose `SUPABASE_SERVICE_ROLE_KEY` in the browser. The seed script uses the existing tables only; it does not create Auth users or database tables.

Supabase Email Auth is used for signup, login, logout, and protected API requests. A signup immediately saves the new profile to the existing `users` table, including when email confirmation is enabled; the backend verifies the account against Supabase Auth before saving. If confirmation is required, the app asks the user to confirm their email and then log in. If Supabase returns an active session, signup opens the app directly. Successful logins also upsert the authenticated user ID, email, and display name. This keeps credentials and sessions in Supabase Auth while keeping the CampusConnect profile in your database. Enable the Email provider in Supabase Authentication settings. The `users` table must include `id`, `email`, `name`, and `role` columns.

## Deploy to Vercel

Import this GitHub repository into Vercel using the Python framework preset. Before deploying, add these environment variables to the Vercel project for Production (and Preview if needed). Redeploy after changing environment variables; pushes to the connected `main` branch trigger a production deployment:

```env
SUPABASE_URL
SUPABASE_ANON_KEY
SUPABASE_SERVICE_ROLE_KEY
```

Use the values from Supabase **Project Settings → API**. Keep the service-role key only in Vercel's server-side environment variables; never add it to frontend code or Git. Vercel serves the frontend assets from `public/` and runs the Flask API as a Python Function. After deployment, add the Vercel production URL to Supabase **Authentication → URL Configuration** so email confirmation and redirects work for the deployed app.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/v1/health` | Supabase health check |
| GET | `/api/v1/events` | Published event discovery and filters |
| GET | `/api/v1/events/:id` | Event detail |
| GET/POST | `/api/v1/categories`, `/api/v1/events/:id/register` | Categories and registration |
| GET | `/api/v1/registrations/me` | Current user's registrations |
| GET | `/api/v1/registrations/:id/pass` | Pass and QR data URL |
| GET/PATCH | `/api/v1/users/me` | Profile |
| GET/PUT | `/api/v1/users/me/preferences` | Notification preferences |
| GET/POST/PATCH | `/api/v1/organizer/*`, `/api/v1/events` | Organizer operations |
| GET | `/api/v1/registrations/me/calendar.ics` | Calendar export |

For production, configure the deployed frontend URL under Supabase **Authentication → URL Configuration**. Do not put the service-role key in the browser.
