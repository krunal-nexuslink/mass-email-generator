"""
Shared job store, models, background runner, and endpoints
for mass email generation servers.
"""

import asyncio
import logging
import uuid

import os

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from pydantic import BaseModel, Field
from fastapi.responses import RedirectResponse, JSONResponse
from google_auth_oauthlib.flow import Flow
from google.oauth2.credentials import Credentials
import google.auth.transport.requests
import json
from dotenv import load_dotenv

# Load .env so env vars are available regardless of entry point
load_dotenv()

# ── App + CORS + Sessions ──────────────────────────────────────────────────────

app = FastAPI()

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET_KEY", "dev-secret-change-in-production"),
    same_site="lax",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Job Store ──────────────────────────────────────────────────────────────────

jobs: dict[str, dict] = {}
JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_CANCELLED = "cancelled"
JOB_ERROR = "error"

# ── Function Registry ──────────────────────────────────────────────────────────
# Servers register their LLM-specific functions here for dependency injection.

FUNCTIONS: dict[str, callable] = {
    "generate_email_fn": None,
    "scrape_website_fn": None,
}

# ── OAuth Token Store ───────────────────────────────────────────────────────────

tokens: dict[str, Credentials] = {}


def _is_oauth_configured() -> bool:
    """Check if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are set in environment."""
    return bool(os.getenv("GOOGLE_CLIENT_ID")) and bool(os.getenv("GOOGLE_CLIENT_SECRET"))


async def get_credentials(request: Request) -> Credentials | None:
    """Get OAuth credentials from the current session. Refreshes if expired."""
    session_id = request.session.get("session_id")
    if not session_id or session_id not in tokens:
        return None

    creds: Credentials = tokens[session_id]

    # Refresh if expired
    if creds.expired and creds.refresh_token:
        try:
            creds.refresh(google.auth.transport.requests.Request())
            tokens[session_id] = creds
        except Exception:
            # Refresh failed — token revoked or expired
            tokens.pop(session_id, None)
            request.session.clear()
            return None

    return creds


def html_encode_non_ascii(text: str) -> str:
    """Pre-encode non-ASCII characters as HTML numeric entities (&#NNN;)
    to prevent mojibake in downstream email delivery (Instantly).
    ASCII characters (0-127) are left unchanged."""
    return text.encode("ascii", "xmlcharrefreplace").decode("ascii")


# ── Models ─────────────────────────────────────────────────────────────────────

class MassEmailRequest(BaseModel):
    sender_name: str
    sender_role: str
    sender_objective: str
    google_sheet_url: str
    start_row: int = Field(ge=1, description="First data row (1-based, where 1 = first row after header)")
    end_row: int = Field(ge=1, description="Last data row (inclusive)")
    write_to_sheet: bool = False

# ── Background Runner ──────────────────────────────────────────────────────────


async def _run_mass_generate(
    job_id: str,
    sender_name: str,
    sender_role: str,
    sender_objective: str,
    google_sheet_url: str,
    start_row: int,
    end_row: int,
    write_to_sheet: bool,
    generate_email_fn=None,
    scrape_website_fn=None,
    credentials=None,
):
    """Background job runner. Sets status, calls mass_generate_emails, updates job store."""
    from mass_email_generator import mass_generate_emails

    progress_callback = lambda current, total: _update_progress(job_id, current, total)
    cancel_flag = lambda: jobs[job_id].get("cancel", False)

    try:
        jobs[job_id]["status"] = JOB_RUNNING

        result = await mass_generate_emails(
            sender_name=sender_name,
            sender_role=sender_role,
            sender_objective=sender_objective,
            google_sheet_url=google_sheet_url,
            start_row=start_row,
            end_row=end_row,
            progress_callback=progress_callback,
            cancel_flag=cancel_flag,
            generate_email_fn=generate_email_fn,
            scrape_website_fn=scrape_website_fn,
        )

        # Check if cancelled
        if jobs[job_id].get("cancel"):
            jobs[job_id]["status"] = JOB_CANCELLED
            return

        if write_to_sheet and credentials is not None:
            try:
                from mass_email_generator import _write_results_to_sheet
                write_result = await _write_results_to_sheet(
                    credentials, google_sheet_url, result.get("results", [])
                )
                result["sheet_write_status"] = write_result.get("status", "failed")
                result["sheet_write_error"] = write_result.get("error")
            except Exception as e:
                result["sheet_write_status"] = "failed"
                result["sheet_write_error"] = str(e)
        else:
            result["sheet_write_status"] = "skipped"
            result["sheet_write_error"] = None

        jobs[job_id]["results"] = result
        jobs[job_id]["progress"] = 100
        jobs[job_id]["status"] = JOB_DONE

        # If some rows failed, note it without hiding the successful results
        if result.get("errors", 0) > 0:
            jobs[job_id]["error"] = (
                f"{result['successful']} succeeded, {result['skipped']} skipped, "
                f"{result['errors']} error(s)"
            )

    except RuntimeError as e:
        jobs[job_id]["status"] = JOB_CANCELLED
        jobs[job_id]["error"] = str(e)
    except Exception as e:
        if write_to_sheet and credentials is not None:
            try:
                from mass_email_generator import _write_results_to_sheet
                if jobs[job_id].get("results") and jobs[job_id]["results"].get("results"):
                    await _write_results_to_sheet(
                        credentials, google_sheet_url,
                        jobs[job_id]["results"]["results"],
                    )
            except Exception as sheet_err:
                logging.warning("Failed to write results to sheet for job %s: %s", job_id, sheet_err)
        jobs[job_id]["status"] = JOB_ERROR
        jobs[job_id]["error"] = str(e)


def _update_progress(job_id: str, current: int, total: int):
    jobs[job_id]["current"] = current
    jobs[job_id]["total"] = total
    jobs[job_id]["progress"] = int((current / total) * 100) if total > 0 else 0


# ── Endpoints ──────────────────────────────────────────────────────────────────


@app.post("/mass_generate_email")
async def mass_generate_website_email(req: MassEmailRequest, request: Request):
    """Start a mass generation job and return immediately with job_id."""

    # Validate auth for write_to_sheet
    if req.write_to_sheet:
        creds = await get_credentials(request)
        if creds is None:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required for sheet write-back. Sign in with Google first."},
            )

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "status": JOB_PENDING,
        "progress": 0,
        "current": 0,
        "total": 0,
        "cancel": False,
        "results": None,
        "error": None,
        "credentials": creds if req.write_to_sheet else None,
    }

    asyncio.create_task(_run_mass_generate(
        job_id=job_id,
        sender_name=req.sender_name,
        sender_role=req.sender_role,
        sender_objective=req.sender_objective,
        google_sheet_url=req.google_sheet_url,
        start_row=req.start_row,
        end_row=req.end_row,
        write_to_sheet=req.write_to_sheet,
        generate_email_fn=FUNCTIONS.get("generate_email_fn"),
        scrape_website_fn=FUNCTIONS.get("scrape_website_fn"),
        credentials=creds if req.write_to_sheet else None,
    ))

    return {"job_id": job_id, "status": JOB_PENDING}


@app.get("/mass_generate_email/status/{job_id}")
async def get_job_status(job_id: str):
    """Poll job status and progress."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "job_id": job_id,
        "status": job["status"],
        "progress": job["progress"],
        "total": job.get("total", 0),
        "current": job.get("current", 0),
        "results": job.get("results"),
        "error": job.get("error"),
    }


@app.post("/mass_generate_email/cancel/{job_id}")
async def cancel_job(job_id: str):
    """Cancel a running job."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job["cancel"] = True
    return {"job_id": job_id, "status": "cancelling"}


# ── Auth Endpoints ──────────────────────────────────────────────────────────────


@app.get("/auth/google/login")
async def google_login(request: Request):
    """Redirect user to Google OAuth consent screen."""
    if not _is_oauth_configured():
        return JSONResponse(
            status_code=501,
            content={"error": "OAuth is not configured. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env"},
        )

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:7000/auth/google/callback"],
            }
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    flow.redirect_uri = "http://localhost:7000/auth/google/callback"

    # Enable PKCE and generate code_verifier
    flow.autogenerate_code_verifier = True
    authorization_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
    )

    # Store state and PKCE code_verifier in session
    request.session["oauth_state"] = state
    request.session["code_verifier"] = flow.code_verifier

    return RedirectResponse(url=authorization_url, status_code=302)


@app.get("/auth/google/callback")
async def google_callback(request: Request, code: str = "", state: str = ""):
    """Handle OAuth callback, exchange code for tokens, store credentials."""
    if not _is_oauth_configured():
        return JSONResponse(status_code=501, content={"error": "OAuth not configured"})

    # Verify state matches (CSRF protection)
    stored_state = request.session.get("oauth_state")
    if not stored_state or stored_state != state:
        return JSONResponse(status_code=400, content={"error": "State mismatch. Possible CSRF attack."})

    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": os.getenv("GOOGLE_CLIENT_ID"),
                "client_secret": os.getenv("GOOGLE_CLIENT_SECRET"),
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost:7000/auth/google/callback"],
            }
        },
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    flow.redirect_uri = "http://localhost:7000/auth/google/callback"

    # Restore the PKCE code_verifier from the login step
    flow.code_verifier = request.session.get("code_verifier")

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        # Generate a session_id and store credentials
        session_id = str(uuid.uuid4())
        request.session["session_id"] = session_id
        tokens[session_id] = creds

        # Get user email from token info
        from google.oauth2 import id_token
        req = google.auth.transport.requests.Request()
        try:
            id_info = id_token.verify_oauth2_token(
                creds.id_token, req, os.getenv("GOOGLE_CLIENT_ID")
            )
            request.session["user_email"] = id_info.get("email", "")
        except Exception:
            request.session["user_email"] = "unknown"

    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"Token exchange failed: {str(e)}"})

    # Redirect back to frontend
    return RedirectResponse(url="http://localhost:5173", status_code=302)


@app.get("/auth/status")
async def auth_status(request: Request):
    """Return current authentication status."""
    if not _is_oauth_configured():
        return {"authenticated": False, "email": None, "configured": False}

    creds = await get_credentials(request)
    if creds is None:
        return {"authenticated": False, "email": None, "configured": True}

    return {
        "authenticated": True,
        "email": request.session.get("user_email"),
        "configured": True,
    }


@app.post("/auth/logout")
async def auth_logout(request: Request):
    """Clear session and tokens."""
    session_id = request.session.get("session_id")
    if session_id:
        tokens.pop(session_id, None)
    request.session.clear()
    return {"authenticated": False}
