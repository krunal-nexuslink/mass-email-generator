"""
OpenRouter-backed email generation server.
Mirrors server.py behavior while replacing Groq with OpenRouter via OpenAI client.
"""

import asyncio
import json
import os
import re

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from openai import OpenAI
from pydantic import BaseModel, Field

load_dotenv(override=True)

app = FastAPI()


def _classify_rate_limit_bucket(message: str) -> str:
    """Classify rate-limit scope for easier client handling."""
    text = (message or "").lower()
    day_markers = ["per day", "daily", "tokens per day", "requests per day", "tpd", "rpd"]
    minute_markers = [
        "per minute",
        "tokens per minute",
        "requests per minute",
        "tpm",
        "rpm",
        "minute",
    ]
    if any(marker in text for marker in day_markers):
        return "day"
    if any(marker in text for marker in minute_markers):
        return "minute"
    return "unknown"


OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
)


SYSTEM_PROMPT = """You are a senior B2B strategist writing a cold email from one CXO to another, not a salesperson and not a vendor.

Task:
- Write a highly personalized, insight-driven outreach email for NexusLink, a custom software and AI integration partner, targeting CXOs of IT services or software companies.

What to analyze from receiver website content:
- What they offer and their core value proposition
- Business model and market positioning
- Technical signals: AI, ML, automation, integrations, product maturity
- Growth signals: hiring, new features, case studies, expansion
- Likely pain points: scaling engineering, AI gaps, product velocity, legacy systems

How to align with NexusLink:
- Highlight only 1 to 2 genuinely relevant synergies
- Do not list all services
- Keep the connection strategic and natural, not promotional

Writing requirements:
- Tone: sharp, peer-to-peer, concise, no fluff, no sales voice
- Greet exactly with: Hi <receiver_name>,
- End with sender first name only
- Body length: strictly 120 to 180 words
- No bullet points in email body
- Do not use: "I hope this email finds you well", "introducing", "impressed by", or close synonyms
- Do not use spam words: free, urgent, guarantee, limited, exclusive, act now
- Never mention outsourcing
- Never fabricate sender company name, team size, or location
- Never write bracket placeholders like [my company] or [your company]
- Never generate fake URLs or links

Output format, strict:
- Line 1: Subject: <subject line>
- Line 2: blank
- Line 3 onward: email body only
- No preamble, no extra labels, no commentary outside this format
"""


def scrape_website(url: str) -> str:
    """Fetch homepage and extract meaningful visible text."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = httpx.get(
            url,
            timeout=8,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Could not fetch website: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")

    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    parts = []

    title = soup.find("title")
    if title:
        parts.append(title.get_text(strip=True))

    meta = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if meta and meta.get("content"):
        parts.append(meta["content"].strip())

    for tag in soup.find_all(["h1", "h2"])[:6]:
        text = tag.get_text(strip=True)
        if text:
            parts.append(text)

    for p in soup.find_all("p")[:5]:
        text = p.get_text(strip=True)
        if len(text) > 40:
            parts.append(text)

    content = "\n".join(parts)
    return content[:4000]


def build_user_message(
    sender_name: str,
    sender_role: str,
    sender_objective: str,
    receiver_name: str,
    receiver_domain: str,
    website_content: str,
    company_description: str | None = None,
) -> str:
    """Build dynamic prompt content kept in user message payload."""
    payload = {
        "sender_name": sender_name,
        "sender_role": sender_role,
        "sender_objective": sender_objective,
        "receiver_name": receiver_name,
        "receiver_domain": receiver_domain,
        "website_content": website_content,
        "company_description": company_description or "",
    }
    return "Use this JSON payload to generate the email.\n" + json.dumps(payload, ensure_ascii=True, indent=2)


def _extract_subject_body(text: str) -> dict:
    subject = ""
    body = text.strip()
    match = re.search(r"(?i)subject\s*[:\-]\s*(.+)", body)
    if match:
        subject = match.group(1).strip()
        body = body[match.end() :].strip()
    return {"subject": subject, "body": body}


def _generate_email_sync(user_message: str) -> dict:
    completion = client.chat.completions.create(
        model=OPENROUTER_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        extra_headers={
            "HTTP-Referer": os.getenv("OPENROUTER_SITE_URL", "http://localhost"),
            "X-OpenRouter-Title": os.getenv("OPENROUTER_APP_NAME", "mass-email-generator"),
        },
    )

    content = completion.choices[0].message.content or ""
    return _extract_subject_body(content)


async def generate_email(user_message: str) -> dict:
    """Run blocking OpenRouter call in a worker thread."""
    return await asyncio.to_thread(_generate_email_sync, user_message)


class EmailRequest(BaseModel):
    sender_name: str
    sender_role: str
    sender_objective: str
    receiver_name: str
    receiver_website: str
    company_description: str | None = None


class MassEmailRequest(BaseModel):
    sender_name: str
    sender_role: str
    sender_objective: str
    google_sheet_url: str
    start_row: int = Field(ge=1, description="First data row (1-based, where 1 = first row after header)")
    end_row: int = Field(ge=1, description="Last data row (inclusive)")


@app.post("/generate_email")
async def generate_website_email(req: EmailRequest):
    website_content = scrape_website(req.receiver_website)

    website_failed = not website_content or website_content.startswith("Could not fetch website:")
    effective_website_content = "" if website_failed else website_content
    effective_company_description = (req.company_description or "").strip() if website_failed else ""

    if not effective_website_content and not effective_company_description:
        raise HTTPException(
            status_code=422,
            detail="Could not extract website content and no company_description was provided.",
        )

    user_message = build_user_message(
        sender_name=req.sender_name,
        sender_role=req.sender_role,
        sender_objective=req.sender_objective,
        receiver_name=req.receiver_name,
        receiver_domain=req.receiver_website,
        website_content=effective_website_content,
        company_description=effective_company_description,
    )

    try:
        email = await generate_email(user_message)
    except Exception as e:
        message = str(e)
        lowered = message.lower()
        if "rate limit" in lowered or "too many requests" in lowered or "429" in lowered:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit",
                    "bucket": _classify_rate_limit_bucket(message),
                    "message": message,
                },
            )
        raise HTTPException(status_code=502, detail=f"LLM generation failed: {message}")

    print(f"Generated email for {req.receiver_name} at {req.receiver_website}")
    print("Subject:", email["subject"])
    print("Body:", email["body"])
    return {
        "receiver": req.receiver_name,
        "subject": email["subject"],
        "body": email["body"],
    }


@app.post("/mass_generate_email")
async def mass_generate_website_email(req: MassEmailRequest):
    from mass_email_generator import mass_generate_emails

    credentials_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH")
    if not credentials_path:
        raise HTTPException(status_code=500, detail="GOOGLE_SHEETS_CREDENTIALS_PATH not configured")
    if not os.path.exists(credentials_path):
        raise HTTPException(status_code=500, detail=f"Credentials file not found: {credentials_path}")

    try:
        result = await mass_generate_emails(
            sender_name=req.sender_name,
            sender_role=req.sender_role,
            sender_objective=req.sender_objective,
            google_sheet_url=req.google_sheet_url,
            start_row=req.start_row,
            end_row=req.end_row,
            credentials_path=credentials_path,
        )
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))