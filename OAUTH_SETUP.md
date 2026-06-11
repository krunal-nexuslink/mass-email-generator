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
5. Add Authorized JavaScript origins: `http://localhost:5173`
6. Add Authorized redirect URIs: `http://localhost:7000/auth/google/callback`
7. Click "Create"

### 5. Copy Credentials to .env

After creation, a dialog shows your Client ID and Client Secret. Copy them into your `.env` file:

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

1. Start the backend: `python3 server.py`
2. Start the frontend: `cd ui && npm run dev`
3. Open http://localhost:5173
4. Click "Sign in with Google"
5. After authentication, check "Write results to sheet"
6. Submit a generation job

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "Access blocked: Authorization Error" | Go to the OAuth consent screen and add your email as a Test User |
| "redirect_uri_mismatch" | Verify the redirect URI in GCP matches exactly: `http://localhost:7000/auth/google/callback` |
| Token expired mid-job | The server auto-refreshes tokens. If it fails, check the console for "Token refresh failed" |
| Sheet write-back fails with 403 | Ensure the authenticated Google account has Editor access to the sheet |
