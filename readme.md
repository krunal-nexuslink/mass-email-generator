# Mass Email Generator

Personalized cold-email generation at scale. Given a Google Sheet with receiver data (name, domain, company description), this tool scrapes each company's website, feeds the content + a strategic prompt into an LLM (Groq or OpenRouter), and writes personalized emails back to the sheet.

## Project Structure

```
├── server.py              # FastAPI app — Groq-backed LLM endpoint
├── server_openrouter.py   # Alternative — OpenRouter-backed LLM endpoint
├── server_shared.py       # Shared framework: CORS, OAuth, job store, models
├── mass_email_generator.py# Core orchestration: reads sheets, generates in batch
├── requirements.txt       # Python dependencies
├── ui/                    # Vite-powered frontend (vanilla JS)
│   ├── index.html
│   ├── app.js
│   ├── style.css
│   ├── vite.config.js
│   └── package.json
├── OAUTH_SETUP.md         # Step-by-step Google OAuth setup guide
├── .env.example           # Template — copy to .env and fill in
├── .gitignore
└── old_files/             # Legacy scripts and data (kept for reference)
```

## Quick Start

### Prerequisites

- Python 3.10+
- Node.js 18+
- A Groq API key (free tier available at https://console.groq.com) or an OpenRouter API key

### 1. Clone & Setup

```bash
git clone <repo-url> mass-email-generator
cd mass-email-generator

# Backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Frontend
cd ui
npm install
cd ..
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```bash
GROQ_API_KEY=gsk_...                 # Your Groq API key
# OR
OPENROUTER_API_KEY=sk-or-v1-...      # Your OpenRouter API key
```

For Google Sheets write-back support, also fill in `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, and `SESSION_SECRET_KEY`. See [OAUTH_SETUP.md](OAUTH_SETUP.md) for detailed instructions.

### 3. Run the Backend

**With Groq (recommended for lower latency):**

```bash
uvicorn server:app --host 0.0.0.0 --port 7000
```

**With OpenRouter (broader model selection):**

```bash
uvicorn server_openrouter:app --host 0.0.0.0 --port 7000
```

### 4. Run the Frontend (Development)

```bash
cd ui
npm run dev
```

Opens at http://localhost:5173 by default. The Vite dev server proxies `/mass_generate_email` and `/auth` requests to the backend (configurable via `VITE_API_URL` in `.env`).

### 5. Generate Emails

1. Open the frontend in your browser
2. Fill in the sender details (name, role, objective)
3. Paste a **public** Google Sheet URL with columns: `First name`, `Company domain`, `Company description`
4. Set the row range
5. Click "Generate Emails"

## Configuration Reference

All configuration is via environment variables in `.env`:

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API key for server.py |
| `OPENROUTER_API_KEY` | — | OpenRouter API key for server_openrouter.py |
| `OPENROUTER_MODEL` | `meta-llama/llama-3.3-70b-instruct` | OpenRouter model override |
| `EMAIL_GENERATION_GPU_SERVER_PATH` | `http://localhost:7000/generate_email` | External GPU server endpoint |
| `GOOGLE_CLIENT_ID` | — | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | — | Google OAuth client secret |
| `SESSION_SECRET_KEY` | — | Session encryption key (use `openssl rand -hex 32`) |
| `BACKEND_HOST` | `localhost` | Backend hostname (used in OAuth redirect URI) |
| `BACKEND_PORT` | `7000` | Backend port (used in OAuth redirect URI) |
| `BACKEND_URL` | `http://{BACKEND_HOST}:{BACKEND_PORT}` | Override for the full backend URL (use when behind a reverse proxy) |
| **`FRONTEND_URL`** | `http://localhost:5173` | **Frontend URL for CORS and OAuth post-login redirect** |
| `VITE_API_URL` | `http://localhost:7000` | Vite dev server proxy target |

### Changing the Frontend Port

1. Set `FRONTEND_URL` in `.env` to the new URL (e.g., `http://localhost:3000`)
2. Update `BACKEND_URL` if the backend is also moving
3. Start the Vite dev server with the new port:
   ```bash
   cd ui && npx vite --port 3000
   ```
4. Update the Google Cloud Console OAuth credentials:
   - **Authorized JavaScript origins** → add your new frontend URL
   - **Authorized redirect URIs** → add `{BACKEND_URL}/auth/google/callback`
   See [OAUTH_SETUP.md](OAUTH_SETUP.md) for details.

## Server Variants

The project includes two server implementations that share the same API surface:

| File | LLM Backend | When to Use |
|---|---|---|
| `server.py` | Groq (`llama-3.3-70b-versatile`) | Lower latency, simpler setup |
| `server_openrouter.py` | OpenRouter (meta-llama/llama-3.3-70b-instruct) | Broader model selection, rate-limit headroom |

Run only one at a time — they register the same routes on the same `FastAPI` app in `server_shared.py`.

## Production Deployment

### Building the Frontend

```bash
cd ui
npm run build    # outputs to ui/dist/
```

You can serve `ui/dist/` from any static file server or wire it into the FastAPI app as a static mount.

### Reverse Proxy Setup

When deploying behind nginx or Caddy:

1. Set `BACKEND_URL` to the public URL of your backend (e.g., `https://api.example.com`)
2. Set `FRONTEND_URL` to the public URL of your frontend (e.g., `https://app.example.com`)
3. Update Google Cloud Console OAuth credentials with the production URIs
4. Run uvicorn bound to localhost only (the reverse proxy handles public traffic):
   ```bash
   uvicorn server:app --host 127.0.0.1 --port 7000
   ```

## Old / Legacy Files

Non-essential scripts and data files have been moved to `old_files/` for reference:

- `generate_email.py` / `generate_email_krunal.py` — Standalone batch scripts with hardcoded paths
- `test_emailgen.py` — Quick test for the standalone scripts
- `*.csv` — Sample input/output data files
- `Mass_email_generator_client_secret_*.json` — Previously leaked credential file (gitignored)
- `readme.md` — Previous empty readme

These are kept for reference but not required to run the application.
