"""
Mass email generator — reads receiver data from a public Google Sheet,
generates personalized cold emails using existing functions from server.py,
and writes results back to the sheet.
"""

import asyncio
import logging
import re
from collections.abc import Callable

import pandas as pd

# ── Type aliases for dependency injection ─────────────────────────────────────

GenerateEmailFn = Callable[[str, str, str, str, str, str, str], tuple[str, str]]
"""Args: sender_name, sender_role, sender_objective, receiver_name,
receiver_domain, website_content, company_description. Returns (subject, body)."""

ScrapeWebsiteFn = Callable[[str], str]
"""Args: url. Returns website content string."""

logger = logging.getLogger(__name__)


# ── Sheet helpers ──────────────────────────────────────────────────────────────


def _parse_sheet_url(url: str) -> tuple[str, int]:
    """Extract (spreadsheet_id, gid) from a Google Sheets URL.

    Supports formats:
    - https://docs.google.com/spreadsheets/d/{ID}/edit#gid={GID}
    - https://docs.google.com/spreadsheets/d/{ID}/edit
    - https://docs.google.com/spreadsheets/d/{ID}
    """
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if not match:
        raise ValueError(f"Could not parse spreadsheet ID from URL: {url}")
    sheet_id = match.group(1)

    gid = 0
    gid_match = re.search(r"#gid=(\d+)", url)
    if gid_match:
        gid = int(gid_match.group(1))

    return sheet_id, gid


def _read_sheet_csv(sheet_id: str, gid: int = 0) -> pd.DataFrame:
    """Read a public Google Sheet via CSV export URL. No auth needed.

    URL format: https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv
    Append &gid={gid} if gid != 0.
    """
    url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"
    if gid != 0:
        url += f"&gid={gid}"
    logger.info("Reading sheet from CSV export URL")
    return pd.read_csv(url)


def _get_csv_path(sheet_url: str) -> str:
    """Derive a CSV filename from the Google Sheet URL."""
    sheet_id, _ = _parse_sheet_url(sheet_url)
    return f"mass_email_output_{sheet_id}.csv"





# ── Row processing ─────────────────────────────────────────────────────────────


async def _process_single_row(
    sender_name: str,
    sender_role: str,
    sender_objective: str,
    row: pd.Series,
    generate_email_fn: GenerateEmailFn | None = None,
    scrape_website_fn: ScrapeWebsiteFn | None = None,
) -> tuple[str, str]:
    """Process one sheet row and return (subject, body).

    1. Extract First name + Last name -> receiver_name
    2. Extract Company domain -> receiver_domain
    3. Extract Company description -> company_desc_raw
    4. scrape_website(domain) via asyncio.to_thread() (sync wrapper)
    5. If scrape fails and company_description exists -> use as fallback, website_content=""
    6. If scrape fails and no company_description -> return ("SKIPPED", "") sentinel
    7. build_prompt(...) — sync, fast
    8. await generate_email(filled_prompt) — already async
    9. Return (subject, body)

    Raises ValueError if First name or Company domain is missing.
    Returns ("SKIPPED", "") if both website scrape and company description are unavailable.

    When generate_email_fn / scrape_website_fn are provided, they are used directly.
    When omitted, the function falls back to importing from server.py (backward compat).
    """
    # ── Resolve functions: injected or fall back to server imports ──────────
    if generate_email_fn is not None and scrape_website_fn is not None:
        _generate_email = generate_email_fn
        _scrape_website = scrape_website_fn
    else:
        import server

        async def _generate_email(
            sn: str, sr: str, so: str, rn: str, rd: str, wc: str, cd: str
        ) -> tuple[str, str]:
            prompt = server.build_prompt(sn, sr, so, rn, rd, wc, cd)
            result = await server.generate_email(prompt)
            return result["subject"], result["body"]

        _scrape_website = server.scrape_website  # sync Callable[[str], str]

    # ── Extract row data ────────────────────────────────────────────────────
    first_name = row.get("First name", "")
    last_name = row.get("Last name", "")
    domain = row.get("Company domain", "")
    company_desc_raw = row.get("Company description", "")

    if pd.isna(first_name) or not str(first_name).strip():
        raise ValueError("Missing First name")
    if pd.isna(domain) or not str(domain).strip():
        raise ValueError("Missing Company domain")

    receiver_name = str(first_name).strip()
    if not pd.isna(last_name) and str(last_name).strip():
        receiver_name += " " + str(last_name).strip()
    receiver_domain = str(domain).strip()

    logger.info("Processing: %s <%s>", receiver_name, receiver_domain)

    cd = str(company_desc_raw).strip() if not pd.isna(company_desc_raw) else ""

    # _scrape_website is sync — run in thread to avoid blocking the event loop
    website_content = await asyncio.to_thread(_scrape_website, receiver_domain)

    website_failed = not website_content or website_content.startswith(
        "Could not fetch website:"
    )

    if website_failed:
        if not cd:
            logger.warning(
                "Skipping %s <%s>: website scrape failed and no Company description",
                receiver_name,
                receiver_domain,
            )
            return ("SKIPPED", "")
        logger.info(
            "Website scrape failed, using Company description for %s",
            receiver_name,
        )
        website_content = ""

    subject, body = await _generate_email(
        sender_name, sender_role, sender_objective,
        receiver_name, receiver_domain, website_content, cd,
    )
    return subject, body


# ── Main orchestrator ──────────────────────────────────────────────────────────


async def mass_generate_emails(
    sender_name: str,
    sender_role: str,
    sender_objective: str,
    google_sheet_url: str,
    start_row: int,
    end_row: int,
    batch_size: int = 3,
    save_every: int = 10,
    progress_callback: Callable | None = None,
    cancel_flag: Callable | None = None,
    generate_email_fn: GenerateEmailFn | None = None,
    scrape_website_fn: ScrapeWebsiteFn | None = None,
) -> dict:
    """Main orchestrator for mass email generation.

    Row mapping:
    - Sheet row 1 = header (First name, Last name, Company domain, Company description,
      generated_email_subject, generated_email_body)
    - User's start_row=1 -> first data row = sheet row 2 -> pandas index 0
    - pandas slice: df.iloc[start_row-1 : end_row]  (end_row is exclusive)

    Algorithm:
    1. Parse sheet URL -> sheet_id, gid
    2. Read sheet CSV -> DataFrame, initialize output columns
    3. Validate row range
    4. For each batch of batch_size rows (concurrent via asyncio.gather):
       a. Create tasks for each row
       b. await asyncio.gather(*tasks, return_exceptions=True)
       c. Check for exceptions — if any, raise RuntimeError with sheet row and error details
       d. Write successful results to DataFrame, save to CSV every `save_every` rows
    5. Final CSV save
    6. Return dict with: status, total_requested, successful count, results list, csv_path
    """
    # 1. Parse sheet URL
    sheet_id, gid = _parse_sheet_url(google_sheet_url)
    logger.info("Parsed sheet: id=%s, gid=%d", sheet_id, gid)

    # 2. Read sheet CSV
    df = _read_sheet_csv(sheet_id, gid)
    logger.info("Read %d rows from sheet", len(df))

    for col in ["generated_email_subject", "generated_email_body"]:
        if col not in df.columns:
            df[col] = ""
        else:
            df[col] = df[col].astype("string")

    # 3. Validate row range
    total_requested = end_row - start_row + 1
    start_idx = start_row - 1
    end_idx = end_row  # exclusive in iloc

    if start_idx < 0 or end_idx > len(df) or start_idx >= end_idx:
        raise ValueError(
            f"Invalid row range: start_row={start_row}, end_row={end_row}. "
            f"DataFrame has {len(df)} data rows (sheet rows 2..{len(df) + 1})."
        )

    csv_path = _get_csv_path(google_sheet_url)
    logger.info("Output CSV: %s", csv_path)

    rows_to_process = df.iloc[start_idx:end_idx]
    results: list[dict] = []
    successful = 0
    skipped = 0
    processed_since_save = 0
    error_details: list[dict] = []

    # 4. Process in batches
    for batch_start in range(0, len(rows_to_process), batch_size):
        # Check cancellation before processing this batch
        if cancel_flag and cancel_flag():
            logger.warning("Job cancelled by user")
            raise RuntimeError("Job cancelled by user")

        batch_rows = rows_to_process.iloc[batch_start : batch_start + batch_size]
        logger.info(
            "Processing batch: data rows %d-%d",
            batch_start + start_row,
            batch_start + start_row + len(batch_rows) - 1,
        )

        tasks = [
            _process_single_row(
                sender_name, sender_role, sender_objective, row,
                generate_email_fn=generate_email_fn,
                scrape_website_fn=scrape_website_fn,
            )
            for _, row in batch_rows.iterrows()
        ]

        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        for idx, (pandas_index, row) in enumerate(batch_rows.iterrows()):
            sheet_row = pandas_index + 2
            result_or_error = batch_results[idx]

            if isinstance(result_or_error, Exception):
                first_name = row.get("First name", "")
                domain = row.get("Company domain", "")
                logger.error(
                    "Skipping sheet row %d (name=%s, domain=%s): %s",
                    sheet_row, first_name, domain, result_or_error,
                )
                error_details.append({
                    "sheet_row": sheet_row,
                    "name": first_name,
                    "domain": domain,
                    "error": str(result_or_error),
                })
                continue

            subject, body = result_or_error

            if subject == "SKIPPED":
                logger.warning("Sheet row %d: no context available, writing N/A", sheet_row)
                df.at[pandas_index, "generated_email_subject"] = "N/A"
                df.at[pandas_index, "generated_email_body"] = "N/A"
                results.append({
                    "sheet_row": sheet_row,
                    "subject": "N/A",
                    "body": "N/A",
                    "status": "skipped_no_context",
                })
                skipped += 1
            else:
                df.at[pandas_index, "generated_email_subject"] = subject
                df.at[pandas_index, "generated_email_body"] = body
                results.append({
                    "sheet_row": sheet_row,
                    "subject": subject,
                    "body": body,
                })
                successful += 1
                processed_since_save += 1

                if processed_since_save >= save_every:
                    df.to_csv(csv_path, index=False)
                    logger.info("Saved %d rows to %s", processed_since_save, csv_path)
                    processed_since_save = 0

            # Report progress after this row
            if progress_callback:
                progress_callback(successful + skipped, total_requested)

    # Final save
    df.to_csv(csv_path, index=False)
    logger.info("Final save: %d rows written to %s", len(rows_to_process), csv_path)

    has_errors = len(error_details) > 0
    if has_errors:
        logger.warning(
            "Completed with %d error(s): %s", len(error_details),
            "; ".join(e["error"][:80] for e in error_details),
        )

    return {
        "status": "partial" if has_errors else "success",
        "total_requested": total_requested,
        "successful": successful,
        "skipped": skipped,
        "errors": len(error_details),
        "error_details": error_details if has_errors else None,
        "csv_path": csv_path,
        "results": results,
    }


# ── Sheet writer ──────────────────────────────────────────────────────────────


async def _write_results_to_sheet(
    credentials,
    sheet_url: str,
    results: list[dict],
    gid: int | None = None,
) -> dict:
    """Write generated email results back to the Google Sheet using gspread.

    Args:
        credentials: google.oauth2.credentials.Credentials object
        sheet_url: Full Google Sheets URL
        results: List of dicts with keys: sheet_row, subject, body, status
        gid: Optional gid override (parsed from URL if not provided)

    Returns:
        dict with keys: status, rows_written, error
    """
    import gspread
    from google.auth.transport.requests import Request

    try:
        sheet_id, parsed_gid = _parse_sheet_url(sheet_url)
    except Exception as e:
        return {"status": "failed", "rows_written": 0, "error": f"Failed to parse sheet URL: {e}"}

    target_gid = gid if gid is not None else parsed_gid

    if credentials.expired and credentials.refresh_token:
        try:
            credentials.refresh(Request())
        except Exception as e:
            return {"status": "failed", "rows_written": 0, "error": f"Token refresh failed: {e}"}

    try:
        gc = gspread.authorize(credentials)
        sh = gc.open_by_key(sheet_id)

        if target_gid != 0:
            worksheet = sh.get_worksheet_by_id(target_gid)
        else:
            worksheet = sh.get_worksheet(0)

        if worksheet is None:
            return {"status": "failed", "rows_written": 0, "error": "Worksheet not found"}

        header = worksheet.row_values(1)

        col_subject = None
        col_body = None
        for i, col_name in enumerate(header):
            col_name_stripped = col_name.strip().lower()
            if col_name_stripped == "generated_email_subject":
                col_subject = i + 1
            elif col_name_stripped == "generated_email_body":
                col_body = i + 1

        if col_subject is None:
            col_subject = len(header) + 1
            worksheet.update_cell(1, col_subject, "generated_email_subject")

        if col_body is None:
            col_body = max(len(header) + (1 if col_subject is None else 2), col_subject + 1)
            worksheet.update_cell(1, col_body, "generated_email_body")

        rows_written = 0
        for entry in results:
            sheet_row = entry.get("sheet_row", 0)
            if sheet_row < 2:
                continue

            subject = entry.get("subject", "")
            body = entry.get("body", "")
            status = entry.get("status", "")

            if status == "skipped_no_context" or subject == "N/A":
                subject = "N/A"
                body = "N/A"

            try:
                worksheet.update_cell(sheet_row, col_subject, subject)
                worksheet.update_cell(sheet_row, col_body, body)
                rows_written += 1
            except Exception as cell_err:
                err_str = str(cell_err).lower()
                if "429" in err_str or "rate limit" in err_str:
                    await asyncio.sleep(1)
                    try:
                        worksheet.update_cell(sheet_row, col_subject, subject)
                        worksheet.update_cell(sheet_row, col_body, body)
                        rows_written += 1
                    except Exception:
                        pass
                elif "401" in err_str or "unauthorized" in err_str:
                    if credentials.expired:
                        credentials.refresh(Request())
                    try:
                        worksheet.update_cell(sheet_row, col_subject, subject)
                        worksheet.update_cell(sheet_row, col_body, body)
                        rows_written += 1
                    except Exception:
                        pass
                else:
                    pass

        return {
            "status": "success" if rows_written > 0 else "failed",
            "rows_written": rows_written,
            "error": None,
        }

    except gspread.exceptions.APIError as e:
        return {"status": "failed", "rows_written": 0, "error": f"Google Sheets API error: {e}"}
    except gspread.exceptions.SpreadsheetNotFound:
        return {"status": "failed", "rows_written": 0, "error": "Spreadsheet not found. Check the URL."}
    except gspread.exceptions.WorksheetNotFound:
        return {"status": "failed", "rows_written": 0, "error": "Worksheet not found."}
    except Exception as e:
        return {"status": "failed", "rows_written": 0, "error": f"Unexpected error: {e}"}
