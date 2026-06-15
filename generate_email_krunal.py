import logging
import os
import json
import time

import pandas as pd
import requests
from dotenv import load_dotenv

log_dir = os.path.join(os.path.dirname(__file__), "logs")
os.makedirs(log_dir, exist_ok=True)
log_path = os.path.join(log_dir, "website_email_gen.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.FileHandler(log_path, encoding="utf-8"),
        logging.StreamHandler()
    ]
)

load_dotenv(override=True)
# print("Environment variables loaded successfully.")
csv_path = "/home/nls189/Documents/Projects/mass_email_generator_krunal/mass-email-generator/CXOs data - email generation based on website  - EU-Austria-Germany-Swiss-IT-Finance-04-06-26.csv"
df = pd.read_csv(
    csv_path,
    dtype={
        "generated_email_subject": "string",
        "generated_email_body": "string",
    }
    )  

URL = os.getenv("EMAIL_GENERATION_GPU_SERVER_PATH")
# URL = os.getenv('WEBSITE_BASED_EMAIL_GENERATOR_URL')

save_every = 10
processed_since_save = 0
max_rows = 2513

# Keep headroom under org limit (30 RPM) to reduce accidental bursts.
max_requests_per_minute = 27
min_interval_seconds = 60.0 / max_requests_per_minute
last_request_time = 0.0

checkpoint_path = os.path.join(os.path.dirname(csv_path), ".generate_email_checkpoint.json")


def load_checkpoint() -> int:
    if not os.path.exists(checkpoint_path):
        return 0
    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return int(data.get("next_row_index", 0))
    except Exception:
        return 0


def save_checkpoint(next_row_index: int) -> None:
    data = {
        "next_row_index": next_row_index,
        "updated_at_epoch": int(time.time()),
    }
    with open(checkpoint_path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def clear_checkpoint() -> None:
    if os.path.exists(checkpoint_path):
        os.remove(checkpoint_path)


def parse_rate_limit_bucket(response: requests.Response) -> tuple[str, str]:
    """Return ('minute'|'day'|'unknown', message)."""
    message = response.text
    bucket = "unknown"

    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            bucket = str(detail.get("bucket") or "unknown")
            message = str(detail.get("message") or message)
        elif detail is not None:
            message = str(detail)

    lowered = message.lower()
    if bucket == "unknown":
        if any(x in lowered for x in ["per day", "daily", "tokens per day", "requests per day", "tpd", "rpd"]):
            bucket = "day"
        elif any(x in lowered for x in ["per minute", "tokens per minute", "requests per minute", "tpm", "rpm"]):
            bucket = "minute"

    return bucket, message


def wait_until_next_minute() -> None:
    now = time.time()
    sleep_for = 61 - (int(now) % 60)
    logging.warning("Minute rate limit reached. Waiting %ss before retry.", sleep_for)
    time.sleep(sleep_for)


def normalize_company_description(value, row_idx: int, context: str = "") -> str:
    """Return empty string for missing descriptions and log it."""
    if pd.isna(value):
        prefix = f"[{context}] " if context else ""
        logging.warning("%sCompany description is missing at row %d. Using empty string.", prefix, row_idx)
        return ""
    return str(value)


def is_no_context_422(response: requests.Response) -> bool:
    """True when server reports both website content and company description are unavailable."""
    if response.status_code != 422:
        return False

    detail_text = response.text
    try:
        payload = response.json()
        if isinstance(payload, dict):
            detail_text = str(payload.get("detail") or detail_text)
    except ValueError:
        pass

    lowered = detail_text.lower()
    return "could not extract website content" in lowered and "no company_description" in lowered


def test_rows_without_csv_write(start_row: int, end_row: int) -> list[dict]:
    """Send requests for an inclusive row range without mutating dataframe/checkpoint/csv."""
    if start_row < 0 or end_row < 0:
        raise ValueError("start_row and end_row must be >= 0")
    if end_row < start_row:
        raise ValueError("end_row must be >= start_row")

    results: list[dict] = []
    local_last_request_time = 0.0

    for idx, row in df.iloc[start_row:end_row + 1].iterrows():
        receiver_full_name = row["First name"]
        receiver_domain = row["Company domain"]
        receiver_company_description = normalize_company_description(
            row["Company description"],
            idx,
            context="TEST",
        )

        if pd.isna(receiver_full_name) or pd.isna(receiver_domain):
            logging.warning("[TEST] Skipping row %d due to missing values.", idx)
            results.append({"row": int(idx), "status": "skipped_missing_values"})
            continue

        payload = {
            "sender_name": "Prem Anjwani",
            "sender_role": "Co-founder",
            "sender_objective": """
                                You are a B2B email writer for NexusLink Services (nexuslinkservices.com), a custom software company having experience in working alongside as partners with IT companies, various industries like logistics, fleet management, and warehouse management, healthcare, Realestate, Financial institutions across Europe and US build operational software and automations to ease their manual work.
Use receiver's website or details to write a personalized email:
Hi [First Name],
One hook line. No "impressed with" statements.
One specific observation about their role and company. Not a compliment.
"At NexusLink we have been helping..." - one specific relevant build, no feature lists.
"I am curious whether..." - one specific open-ended question about their operations.
End with: "Happy to have a short conversation."
Rules: No em dashes. No bullets. No bold. Never mention team size or experience. No "leading provider" or "solutions". Subject 6 to 9 words. Body 130 to 180 words. Peer-to-peer tone.
                            """,
            "receiver_name": receiver_full_name,
            "receiver_website": receiver_domain,
            "company_description": receiver_company_description,
        }

        while True:
            elapsed = time.time() - local_last_request_time
            if elapsed < min_interval_seconds:
                time.sleep(min_interval_seconds - elapsed)

            response = requests.post(URL, json=payload, timeout=90)
            local_last_request_time = time.time()
            logging.info("[TEST] Response for row %d: %d", idx, response.status_code)

            if response.status_code == 200:
                data = response.json()
                results.append(
                    {
                        "row": int(idx),
                        "status": "ok",
                        "subject": str(data.get("subject") or ""),
                        "body": str(data.get("body") or ""),
                    }
                )
                break

            if response.status_code == 429:
                bucket, cause = parse_rate_limit_bucket(response)
                if bucket == "minute":
                    logging.warning("[TEST] Rate limit (minute) at row %d: %s", idx, cause)
                    wait_until_next_minute()
                    continue
                if bucket == "day":
                    logging.error("[TEST] Rate limit (day) at row %d: %s", idx, cause)
                    raise RuntimeError(
                        f"Daily limit reached at row {idx}. Cause: {cause}. "
                        "Switch to a different org/project key and rerun."
                    )

                logging.error("[TEST] Rate limit (unknown bucket) at row %d: %s", idx, cause)
                raise RuntimeError(f"Rate limit at row {idx}. Cause: {cause}")

            if is_no_context_422(response):
                logging.warning(
                    "[TEST] Skipping row %d because website scraping failed and company description is missing.",
                    idx,
                )
                results.append(
                    {
                        "row": int(idx),
                        "status": "skipped_no_context",
                    }
                )
                break

            logging.error(
                "[TEST] Request failed at row %d with status %d: %s",
                idx,
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Request failed at row {idx} with status {response.status_code}. "
                f"Response: {response.text}"
            )

    return results

def batch_email_generation(start_row: int, end_row: int) -> None:
    start_idx = load_checkpoint()
    if start_idx > 0:
        logging.info("Resuming from row index %d based on checkpoint", start_idx)
    else:
        logging.info("No checkpoint found; starting from row index 0")

    try:
        for idx, row in df.iloc[start_idx:max_rows].iterrows():
            logging.debug("Processing row %d...", idx)
            receiver_full_name = row['First name']
            receiver_domain = row['Company domain']
            receiver_company_description = normalize_company_description(
                row['Company description'],
                idx,
            )

            if pd.isna(receiver_full_name) or pd.isna(receiver_domain):
                logging.warning(f"Skipping row {idx} due to missing values.")
                df.at[idx, 'generated_email_subject'] = "N/A"
                df.at[idx, 'generated_email_body'] = "N/A"
                save_checkpoint(idx + 1)
                continue

            logging.info(f"Processing row {idx}: {receiver_full_name} <{receiver_domain}>")
            payload = {
                "sender_name": "Prem Anjwani",
                "sender_role": "Co-founder",
                "sender_objective": """
                                    You are a B2B email writer for NexusLink Services (nexuslinkservices.com), a custom software company having experience in working alongside as partners with IT companies, various industries like logistics, fleet management, and warehouse management, healthcare, Realestate, Financial institutions across Europe and US build operational software and automations to ease their manual work.
    Use receiver's website or details to write a personalized email:
    Hi [First Name],
    One hook line. No "impressed with" statements.
    One specific observation about their role and company. Not a compliment.
    "At NexusLink we have been helping..." - one specific relevant build, no feature lists.
    "I am curious whether..." - one specific open-ended question about their operations.
    End with: "Happy to have a short conversation."
    Rules: No em dashes. No bullets. No bold. Never mention team size or experience. No "leading provider" or "solutions". Subject 6 to 9 words. Body 130 to 180 words. Peer-to-peer tone.
                                """,
                "receiver_name": receiver_full_name,
                "receiver_website": receiver_domain,
                "company_description": receiver_company_description
            }

            while True:
                elapsed = time.time() - last_request_time
                if elapsed < min_interval_seconds:
                    time.sleep(min_interval_seconds - elapsed)

                response = requests.post(URL, json=payload, timeout=180)
                last_request_time = time.time()
                logging.info("Response for row %d: %s", idx, response.status_code)
                logging.debug("Response body for row %d: %s", idx, response.text[:500])

                if response.status_code == 200:
                    data = response.json()
                    subject = str(data.get("subject") or "")
                    body = str(data.get("body") or "")
                    df.at[idx, "generated_email_subject"] = subject
                    df.at[idx, "generated_email_body"] = body
                    save_checkpoint(idx + 1)
                    processed_since_save += 1

                    if processed_since_save >= save_every:
                        df.to_csv(csv_path, index=False)
                        processed_since_save = 0
                    break

                if response.status_code == 429:
                    bucket, cause = parse_rate_limit_bucket(response)
                    if bucket == "minute":
                        logging.warning("Rate limit (minute) at row %d: %s", idx, cause)
                        wait_until_next_minute()
                        continue

                    if bucket == "day":
                        logging.error("Rate limit (day) at row %d: %s", idx, cause)
                        raise RuntimeError(
                            f"Daily limit reached at row {idx}. Cause: {cause}. "
                            "Switch to a different org/project key and rerun to resume from checkpoint."
                        )

                    logging.error("Rate limit (unknown bucket) at row %d: %s", idx, cause)
                    raise RuntimeError(f"Rate limit at row {idx}. Cause: {cause}")

                if is_no_context_422(response):
                    logging.warning(
                        "Skipping row %d because website scraping failed and company description is missing.",
                        idx,
                    )
                    df.at[idx, "generated_email_subject"] = "N/A"
                    df.at[idx, "generated_email_body"] = "N/A"
                    save_checkpoint(idx + 1)
                    processed_since_save += 1

                    if processed_since_save >= save_every:
                        df.to_csv(csv_path, index=False)
                        processed_since_save = 0
                    break

                logging.error("Request failed at row %d with status %d: %s", idx, response.status_code, response.text)
                raise RuntimeError(
                    f"Request failed at row {idx} with status {response.status_code}. "
                    f"Response: {response.text}"
                )
    finally:
        df.to_csv(csv_path, index=False)

    if start_idx < max_rows:
        clear_checkpoint()

if __name__ == "__main__":
    logging.info("Email generation process completed.")
