"""
OpenRouter-backed email generation server.
Mirrors server.py behavior while replacing Groq with OpenRouter via OpenAI client.
"""

import asyncio
import json
import logging
import os
import re

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import HTTPException
from openai import OpenAI
from pydantic import BaseModel

from core import app, jobs, JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_CANCELLED, JOB_ERROR, MassEmailRequest, html_encode_non_ascii, normalize_first_name

load_dotenv(override=True)

_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(_log_dir, exist_ok=True)
_log_path = os.path.join(_log_dir, "website_email_gen.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_path, encoding="utf-8"),
        logging.StreamHandler(),
    ],
    force=True,
)


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
- Use blank lines between the greeting, each body paragraph, and the closing signature
- The email must end EXACTLY with a blank line followed by "Best Regards," on its own line — no text after it, no name, no title, no company, no character at all.
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
    receiver_name = normalize_first_name(receiver_name)
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
    if body:
        body = html_encode_non_ascii(body)
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


# ── Function Registration for Mass Generation ──────────────────────────────────
from core import FUNCTIONS


async def _openrouter_generate_email(sn, sr, so, rn, rd, wc, cd):
    msg = build_user_message(sn, sr, so, rn, rd, wc, cd)
    result = await generate_email(msg)
    return result["subject"], result["body"]


FUNCTIONS["scrape_website_fn"] = scrape_website
FUNCTIONS["generate_email_fn"] = _openrouter_generate_email


class EmailRequest(BaseModel):
    sender_name: str
    sender_role: str
    sender_objective: str
    receiver_name: str
    receiver_website: str
    company_description: str | None = None


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

    logging.info("Generated email for %s at %s", req.receiver_name, req.receiver_website)
    logging.debug("Subject: %s", email["subject"])
    logging.debug("Body: %s", email["body"])
    return {
        "receiver": req.receiver_name,
        "subject": email["subject"],
        "body": email["body"],
    }