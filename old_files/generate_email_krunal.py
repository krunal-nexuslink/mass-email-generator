import asyncio
import json
import logging
import os
import time

import pandas as pd
from dotenv import load_dotenv

from server_openrouter import scrape_website, build_user_message, generate_email

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
csv_path = "/home/nls189/Documents/Projects/mass_email_generator_krunal/mass-email-generator/old_files/CXOs data - email generation based on website  - US-IT_Software development-15-06-26.csv"
df = pd.read_csv(
    csv_path,
    dtype={
        "generated_email_subject": "string",
        "generated_email_body": "string",
    }
    )

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


def normalize_company_description(value, row_idx: int, context: str = "") -> str:
    """Return empty string for missing descriptions and log it."""
    if pd.isna(value):
        prefix = f"[{context}] " if context else ""
        logging.warning("%sCompany description is missing at row %d. Using empty string.", prefix, row_idx)
        return ""
    return str(value)


_SENDER_OBJECTIVE = """
                                You are a B2B email writer for NexusLink Services (nexuslinkservices.com), a custom software company having experience in working alongside as partners with IT companies, various industries like logistics, fleet management, and warehouse management, healthcare, Realestate, Financial institutions across Europe and US build operational software and automations to ease their manual work.
Use receiver's website or details to write a personalized email:
Hi [First Name],
One hook line. No "impressed with" statements.
One specific observation about their role and company. Not a compliment.
"At NexusLink we have been helping..." - one specific relevant build, no feature lists.
"I am curious whether..." - one specific open-ended question about their operations.
End with: "Happy to have a short conversation."
Rules: No em dashes. No bullets. No bold. Never mention team size or experience. No "leading provider" or "solutions". Subject 6 to 9 words. Body 130 to 180 words. Peer-to-peer tone.
                            """


async def test_rows_without_csv_write(start_row: int, end_row: int) -> list[dict]:
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

        website_content = scrape_website(receiver_domain)
        website_failed = not website_content or website_content.startswith("Could not fetch website:")
        effective_website_content = "" if website_failed else website_content
        effective_company_description = receiver_company_description if website_failed else ""

        if not effective_website_content and not effective_company_description:
            results.append({"row": int(idx), "status": "skipped_no_context"})
            continue

        message = build_user_message(
            sender_name="Prem Anjwani",
            sender_role="Co-founder",
            sender_objective=_SENDER_OBJECTIVE,
            receiver_name=receiver_full_name,
            receiver_domain=receiver_domain,
            website_content=effective_website_content,
            company_description=effective_company_description,
        )

        while True:
            elapsed = time.time() - local_last_request_time
            if elapsed < min_interval_seconds:
                time.sleep(min_interval_seconds - elapsed)

            try:
                email = await generate_email(message)
            except Exception as e:
                error_text = str(e).lower()
                if "rate limit" in error_text or "too many requests" in error_text or "429" in error_text:
                    logging.warning("[TEST] Rate limit at row %d: %s", idx, e)
                    time.sleep(61 - (int(time.time()) % 60))
                    continue
                raise RuntimeError(f"Request failed at row {idx}: {e}")

            local_last_request_time = time.time()
            results.append({
                "row": int(idx),
                "status": "ok",
                "subject": email.get("subject", ""),
                "body": email.get("body", ""),
            })
            break

    return results


async def batch_email_generation(start_row: int, end_row: int) -> None:
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

            website_content = scrape_website(receiver_domain)
            website_failed = not website_content or website_content.startswith("Could not fetch website:")
            effective_website_content = "" if website_failed else website_content
            effective_company_description = receiver_company_description if website_failed else ""

            if not effective_website_content and not effective_company_description:
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
                continue

            message = build_user_message(
                sender_name="Prem Anjwani",
                sender_role="Co-founder",
                sender_objective=_SENDER_OBJECTIVE,
                receiver_name=receiver_full_name,
                receiver_domain=receiver_domain,
                website_content=effective_website_content,
                company_description=effective_company_description,
            )

            while True:
                elapsed = time.time() - last_request_time
                if elapsed < min_interval_seconds:
                    time.sleep(min_interval_seconds - elapsed)

                try:
                    email = await generate_email(message)
                except Exception as e:
                    error_text = str(e).lower()
                    if "rate limit" in error_text or "too many requests" in error_text or "429" in error_text:
                        logging.warning("Rate limit (minute) at row %d: %s", idx, e)
                        time.sleep(61 - (int(time.time()) % 60))
                        continue
                    raise RuntimeError(f"Request failed at row {idx}: {e}")

                last_request_time = time.time()
                subject = str(email.get("subject") or "")
                body = str(email.get("body") or "")
                df.at[idx, "generated_email_subject"] = subject
                df.at[idx, "generated_email_body"] = body
                save_checkpoint(idx + 1)
                processed_since_save += 1

                if processed_since_save >= save_every:
                    df.to_csv(csv_path, index=False)
                    processed_since_save = 0
                break
    finally:
        df.to_csv(csv_path, index=False)

    if start_idx < max_rows:
        clear_checkpoint()


if __name__ == "__main__":
    asyncio.run(batch_email_generation(0, max_rows))
