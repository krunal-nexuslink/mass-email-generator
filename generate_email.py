import logging
import os

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

load_dotenv()
print("Environment variables loaded successfully.")

df = pd.read_csv("/home/nls190/workplace/other_scripts/email_generation/website_based_email_generation/7660_records_to_generate_email.csv")

URL = os.getenv("EMAIL_GENERATION_GPU_SERVER_PATH")
# URL = os.getenv('WEBSITE_BASED_EMAIL_GENERATOR_URL')

for idx, row in df.iloc[0:10].iterrows():
    print(f"Processing row {idx}...")
    receiver_full_name = row['First name']
    receiver_domain = row['Company domain']

    if pd.isna(receiver_full_name) or pd.isna(receiver_domain):
        logging.warning(f"Skipping row {idx} due to missing values.")
        df.at[idx, 'generated_email_subject'] = "N/A"
        df.at[idx, 'generated_email_body'] = "N/A"
        continue

    logging.info(f"Processing row {idx}: {receiver_full_name} <{receiver_domain}>")
    payload = {
        "sender_name": "Prem Anjwani",
        "sender_role": "Co-founder",
        "sender_objective": """
                                Write a concise, professional email to a C-level executive in the European IT industry.
                                Frame the intro with genuine awareness of the recipient’s company context without mirroring them.
                                The purpose is to introduce the sender, increase professional visibility, and open a door for natural conversation.
                                Make it clear the sender works with SMBs and product-based companies, helping them build products and contribute to ongoing projects.
                                Use an EU-friendly tone: respectful and understated.
                                Avoid marketing language, ROI framing, or anything that feels like a pitch.
                                The email should feel like one professional reaching out to another out of genuine interest.
                            """,
        "receiver_name": receiver_full_name,
        "receiver_website": receiver_domain
    }

    response = requests.post(URL, json=payload)
    print(f"Response for row {idx}: {response.status_code} - {response.text}")

    if response.status_code == 200:
        print(f"Email generated successfully for row {idx}")
        df.at[idx, 'generated_email_subject'] = response.json().get('subject', '')
        df.at[idx, 'generated_email_body'] = response.json().get('body', '')
    else:
        print(f"Failed to generate email for row {idx}")

# df.to_csv("/home/nls190/workplace/other_scripts/linkedin_email_generation/website_email_gen.csv", index=False)
df.to_csv(
    "/home/nls190/workplace/other_scripts/email_generation/website_based_email_generation/7660_records_to_generate_email.csv"
)