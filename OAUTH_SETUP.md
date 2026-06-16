# Google OAuth2 Setup Guide

## Prerequisites

- A Google account (for the Google Cloud Console)
- Your target Google Sheet(s) should be shared with the Google account you'll authenticate with (Editor permission)

## Step-by-Step

### 1. Create or Select a Google Cloud Project

1. Go to https://console.cloud.google.com/
2. Click the project dropdown at the top, then click "New Project" (or select an existing one)
3. Give it a name (e.g., "Mass Email Generator")
4. Note the Project ID

### 2. Enable the Google Sheets API

1. Go to https://console.cloud.google.com/apis/library/sheets.googleapis.com
2. Make sure your project is selected at the top
3. Click "Enable"

### 3. Configure the OAuth Consent Screen

1. Go to https://console.cloud.google.com/apis/credentials/consent
2. Choose "External" user type, then click "Create"
3. Fill in the required fields:
   - App name: "Mass Email Generator"
   - User support email: your email address
   - Developer contact info: your email address
4. Skip "Scopes" (they will be added via the client), then click "Save and Continue"
5. Skip "Test users", then click "Save and Continue"
6. Review the summary, then click "Back to Dashboard"

### 4. Create OAuth 2.0 Web Client Credentials

1. Go to https://console.cloud.google.com/apis/credentials
2. Click "Create Credentials", then select "OAuth client ID"
3. Set Application type to "Web application"
4. Name: "Mass Email Generator Web Client"
5. **Authorized JavaScript origins**: Enter the frontend URL (default `http://localhost:5173`, configurable via `FRONTEND_URL` in `.env`)
6. **Authorized redirect URIs**: Enter the backend callback URL (default `http://localhost:7000/auth/google/callback`, configurable via `BACKEND_HOST`/`BACKEND_PORT` in `.env`)
7. Click "Create"

### 4a. Edit an Existing OAuth 2.0 Client

If you already have a client ID and need to update its JavaScript origins or redirect URIs:

1. Go to https://console.cloud.google.com/apis/credentials
2. Find your OAuth 2.0 Client ID in the list
3. Click the **pencil/edit icon** on the right side of that entry
4. Under **Authorized JavaScript origins**: click **Add URI** and enter the frontend URL (default `http://localhost:5173`)
5. Under **Authorized redirect URIs**: click **Add URI** and enter the backend callback URL (default `http://localhost:7000/auth/google/callback`)
6. Click **Save** at the bottom

These settings can be updated anytime without creating a new client.

### 5. Copy Credentials to .env

After creation, a dialog shows your Client ID and Client Secret. Copy them into your `.env` file (copy `.env.example` to `.env` first):

```bash
GOOGLE_CLIENT_ID=your-client-id.apps.googleusercontent.com
GOOGLE_CLIENT_SECRET=your-client-secret
SESSION_SECRET_KEY=generate-a-random-string-here
```

**Important:**

- Keep these credentials secret. Never commit them to git.
- Generate a random SESSION_SECRET_KEY. For example: `openssl rand -hex 32`

### 6. Share Your Google Sheet

1. Open your target Google Sheet in the browser
2. Click "Share" in the top-right corner
3. Add the Google account you will sign in with (the same one used to create the OAuth credentials)
4. Give "Editor" permission

### 7. Run the Application

1. Copy `.env.example` to `.env` and fill in your credentials
2. Start the backend: `uvicorn server:app --host 0.0.0.0 --port 7000` (or use `server_openrouter:app` for the OpenRouter variant)
3. Start the frontend: `cd ui && npm run dev`
4. Open the frontend URL (default http://localhost:5173, set via `FRONTEND_URL` in `.env`)
5. Click "Sign in with Google"
6. After authentication, check "Write results to sheet"
7. Submit a generation job

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Access blocked: Authorization Error" | Go to the OAuth consent screen and add your email as a Test User |
| "redirect_uri_mismatch" | Verify the redirect URI in GCP matches exactly: `http://localhost:7000/auth/google/callback` |
| Token expired mid-job | The server auto-refreshes tokens. If it fails, check the console for "Token refresh failed" |
| Sheet write-back fails with 403 | Ensure the authenticated Google account has Editor access to the sheet |
