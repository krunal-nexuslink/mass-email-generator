"""
website_email_gen.py
4 functions: scrape → build prompt → generate → serve
"""

import re
import os

import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import HTTPException
from langchain_core.prompts import PromptTemplate
from langchain_groq import ChatGroq
from pydantic import BaseModel

from server_shared import app, jobs, JOB_PENDING, JOB_RUNNING, JOB_DONE, JOB_CANCELLED, JOB_ERROR, MassEmailRequest, html_encode_non_ascii

load_dotenv(override=True)


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

# ── LLM ───────────────────────────────────────────────────────────────────────

llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    temperature=0.7,
    api_key=os.getenv("GROQ_API_KEY"),
)

# llm = ChatOllama(
#     model=llm_model,
#     temperature=0.7
# )

# ── Prompt ─────────────────────────────────────────────────────────────────────

EMAIL_PROMPT = """You are a senior B2B strategist writing a cold email from one CXO to another — not a salesperson, not a vendor.

Your task: Write a highly personalized, insight-driven outreach email for NexusLink (a custom software and AI integration partner), targeting CXOs of IT services or software companies.

---

STEP 1 — ANALYZE THE RECEIVER'S WEBSITE
Extract and understand:
- What they offer and their core value proposition
- Business model and market positioning
- Technical signals: AI, ML, automation, integrations, product maturity
- Growth signals: hiring, new features, case studies, expansion
- Likely pain points: scaling engineering, AI gaps, product velocity, legacy systems

STEP 2 — IDENTIFY ONE RELEVANT SYNERGY WITH NEXUSLINK
NexusLink does: custom software development, AI/ML integration, product-grade engineering across HealthTech, EdTech, FashionTech, Wellness, Pharma, Field Service Management.
- Highlight ONLY 1–2 synergies that are genuinely relevant to this receiver
- Do NOT list all services
- Make the connection feel natural and strategic, not promotional

STEP 3 — WRITE THE EMAIL

Tone: Sharp, peer-to-peer, concise. No fluff. No sales voice.

STRUCTURE:
- Subject: specific, curiosity-driven, under 80 characters — provide 2–3 options
- Opening: one line referencing something real and specific from their website
- Middle: a brief, thoughtful observation + how NexusLink has helped similar companies
- Closing: one soft line that ends naturally — no ask, no CTA, no commitment request
- Greeting on its own line, followed by a blank line
- Each body paragraph separated by a blank line
- Closing and signature on separate lines with a blank line before signature

STRICT RULES:
- Greet with exactly: Hi {receiver_name},
- End with sender's first name only
- Email body must be 120–180 words — strictly enforce this
- No bullet points inside the email body
- No "I hope this email finds you well", no "introducing", no "impressed by", no synonyms of these
- No spam words: free, urgent, guarantee, limited, exclusive, act now
- Never mention outsourcing
- Never fabricate or assume sender's company name, team size, or location — if not provided, omit entirely
- Never write "[my company]", "[your company]", or any bracketed placeholder
- Never generate fake URLs or links
- Use blank lines between the greeting, each body paragraph, and the closing signature
- In the closing signature, Don't write sender's first name. Just write "Best Regards," in bolds instead.

Sender: {sender_name}
Sender's objective: {sender_objective}

Receiver Name: {receiver_name}
Receiver's Domain: {receiver_domain}
Receiver's Website Info:
{website_content}

Receiver Company Description:
{company_description}

If website content is not found, Use Receiver's Company Description for generating email.

---

 **Output Format — STRICT. Never deviate:**
- Line 1: Subject: <subject line>
- Line 2: blank
- Line 3+: email body

Do NOT add any preamble, label, or commentary outside this format.
"""

# ── 4 Functions ────────────────────────────────────────────────────────────────

def scrape_website(url: str) -> str:
    """Fetch homepage, extract only the meaningful above-the-fold text."""
    if not url.startswith("http"):
        url = "https://" + url
    try:
        resp = httpx.get(url, timeout=8, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
    except Exception as e:
        return f"Could not fetch website: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove noise
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()

    # Pull signal: title, meta description, h1, h2, first few paragraphs
    parts = []

    title = soup.find("title")
    if title:
        parts.append(title.get_text(strip=True))

    meta = soup.find("meta", attrs={"name": "description"}) or \
           soup.find("meta", attrs={"property": "og:description"})
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
    return content[:4000]  # keep it lean


def build_prompt(sender_name: str, sender_role: str, sender_objective: str,
                 receiver_name: str, receiver_domain:str, website_content: str,
                 company_description: str | None = None) -> str:
    """Fill the prompt template with all inputs."""
    prompt = PromptTemplate(
        template=EMAIL_PROMPT,
        input_variables=["sender_name", "sender_role", "sender_objective",
                         "receiver_name", "receiver_domain", "website_content", 
                          "company_description"
                        ],
    )
    return prompt.format(
        sender_name=sender_name,
        sender_role=sender_role,
        sender_objective=sender_objective,
        receiver_name=receiver_name,
        receiver_domain=receiver_domain,
        website_content=website_content,
        company_description=company_description
    )


async def generate_email(filled_prompt: str) -> dict:
    """Call LLM, extract subject and body."""
    result = await llm.ainvoke(filled_prompt)
    text = result.content.strip()

    # Extract subject and body
    subject, body = "", text
    match = re.search(r"(?i)subject\s*[:\-]\s*(.+)", text)
    if match:
        subject = match.group(1).strip()
        body = text[match.end():].strip()

    if body:
        body = html_encode_non_ascii(body)

    return {"subject": subject, "body": body}


# ── Function Registration for Mass Generation ──────────────────────────────────
from server_shared import FUNCTIONS


async def _groq_generate_email(sn, sr, so, rn, rd, wc, cd):
    prompt = build_prompt(sn, sr, so, rn, rd, wc, cd)
    result = await generate_email(prompt)
    return result["subject"], result["body"]


FUNCTIONS["scrape_website_fn"] = scrape_website
FUNCTIONS["generate_email_fn"] = _groq_generate_email

# ── Endpoint ───────────────────────────────────────────────────────────────────

class EmailRequest(BaseModel):
    sender_name: str
    sender_role: str
    sender_objective: str   # what you do / why you're reaching out
    receiver_name: str
    receiver_website: str   # e.g. "kniru.com"
    company_description: str | None = None

@app.post("/generate_email")
async def generate_website_email(req: EmailRequest):
    website_content = scrape_website(req.receiver_website)

    website_failed = (
        not website_content
        or website_content.startswith("Could not fetch website:")
    )
    effective_website_content = "" if website_failed else website_content
    effective_company_description = (req.company_description or "").strip() if website_failed else ""

    if not effective_website_content and not effective_company_description:
        raise HTTPException(
            status_code=422,
            detail="Could not extract website content and no company_description was provided."
        )

    filled_prompt = build_prompt(
        sender_name=req.sender_name,
        sender_role=req.sender_role,
        sender_objective=req.sender_objective,
        receiver_name=req.receiver_name,
        receiver_domain=req.receiver_website,
        website_content=effective_website_content,
        company_description=effective_company_description
    )

    try:
        email = await generate_email(filled_prompt)
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

    return {
        "receiver": req.receiver_name,
        "subject": email["subject"],
        "body": email["body"],
    }


