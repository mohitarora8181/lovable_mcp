# Lovable Landing Pages API

A FastAPI service that builds multiple simple landing pages in one Lovable host
project. Each requested page receives its own route, such as
`https://your-site.lovable.app/coffee-roaster`, while the API prevents updates
to any other page's route folder.

## Requirements

- Python 3.11+
- A Lovable account with access to the host project
- One existing Lovable project, used exclusively as the landing-page host

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Create a `.env` file with:

```dotenv
# Use your registered client ID. Lovable's documented public client ID is the default.
# Leave client ID/secret unset for the recommended Free-account flow. The app
# registers itself as a local MCP OAuth client automatically.
# LOVABLE_OAUTH_CLIENT_ID=
# LOVABLE_OAUTH_CLIENT_SECRET=
# This localhost callback is used by the automatically registered client.
LOVABLE_OAUTH_REDIRECT_URI=http://127.0.0.1:8000/auth/lovable/callback
LOVABLE_PROJECT_ID=the-one-lovable-host-project-id
# Optional; included in create responses.
LOVABLE_BASE_URL=https://your-site.lovable.app
PROJECTS_FILE=data/projects.json
```

Start the service, then open `http://127.0.0.1:8000/auth/lovable/login` in a
browser. Sign in to Lovable and approve the requested access. The API uses
OAuth dynamic client registration for this localhost client, so a custom
Lovable OAuth client key is not needed. Resulting client credentials and
access/refresh tokens are kept locally in `data/lovable_oauth.json` and
refreshed automatically; do not commit that file.

## Run

```powershell
uvicorn app.main:app --reload
```

The interactive API documentation is available at `http://127.0.0.1:8000/docs`.

## API

Create a landing-page route in the configured Lovable project:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/projects `
  -ContentType 'application/json' `
  -Body '{"idea":"A coffee roaster subscription service", "reference_sites":["https://example.com"], "images":[], "urls_to_include":["https://instagram.com/example"], "project_id":"coffee-roaster"}'
```

`project_id` is a route/page identifier, not the actual Lovable project ID. If
omitted, it is derived from `idea`. The example creates `/coffee-roaster`.

Both write endpoints return `202 Accepted` immediately. The server then asks
Lovable to build the change and deploys the shared host project after the build
completes. Keep the API process running until the background task finishes;
success or failure is written to the server log.

Enhance only that landing page:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/projects/coffee-roaster/enhancements `
  -ContentType 'application/json' `
  -Body '{"enhancement":"Add a testimonials section before the final call to action."}'
```

`data/projects.json` stores local page metadata. This is intended for local
development; use a database and encrypted token storage in production.
