"""
register_finder.py

Find Amazon accounts that can be registered using phone numbers from the
firefox.fun SMS verification platform.

Usage:
    # Use a built-in country (e.g. France)
    python -m amazon_crawler.shuler.services.amazon.register_finder \\
        --count 20 --token YOUR_TOKEN --country FR

    # Use any country by specifying PID and dial code manually
    python -m amazon_crawler.shuler.services.amazon.register_finder \\
        --count 20 --token YOUR_TOKEN --pid 1023 --dial-code +44

Built-in countries (PID must be confirmed on firefox.fun before use):
    FR (+33) pid=1017   DE (+49) pid=?   UK (+44) pid=?
    US (+1)  pid=?      JP (+81) pid=?   IT (+39) pid=?
    ES (+34) pid=?      NL (+31) pid=?   PL (+48) pid=?

For countries marked pid=?, you must pass --pid explicitly.
"""
import argparse
import csv
import os
import random
import string
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests
from loguru import logger
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ====================== 接码平台 ======================
SMS_API_BASE = "http://www.firefox.fun/yhapi.ashx"

# country_code → (dial_code, firefox_pid, api_country_param)
# pid=None 表示未确认，需用 --pid 手动指定
# api_country_param 是 firefox.fun getPhone 接口的 country 字段值
COUNTRY_CONFIG = {
    "FR": ("+33",  1017, "fra"),
    "UK": ("+44",  1017, "eng"),
    "GB": ("+44",  1017, "eng"),
    "US": ("+1",   1017, "usa"),
    "DE": ("+49",  None, None),
    "JP": ("+81",  None, None),
    "IT": ("+39",  None, None),
    "ES": ("+34",  None, None),
    "NL": ("+31",  None, None),
    "PL": ("+48",  None, None),
    "BE": ("+32",  None, None),
    "PT": ("+351", None, None),
    "SE": ("+46",  None, None),
    "CA": ("+1",   None, None),
    "AU": ("+61",  None, None),
}

# ====================== Amazon ======================
AMAZON_HOME_URL = "https://www.amazon.com/"


# ====================== 姓名库（按国家） ======================
NAMES_BY_COUNTRY = {
    "FR": (
        ["Emma", "Louis", "Jade", "Lucas", "Lena", "Hugo", "Manon", "Nathan",
         "Camille", "Gabriel", "Alice", "Theo", "Ines", "Raphael", "Lucie",
         "Tom", "Chloe", "Maxime", "Sarah", "Noah", "Julie", "Antoine", "Laura",
         "Paul", "Marie", "Thomas", "Sophie", "Nicolas", "Eva", "Julien"],
        ["Martin", "Bernard", "Thomas", "Petit", "Robert", "Richard", "Durand",
         "Dubois", "Moreau", "Laurent", "Simon", "Michel", "Lefebvre", "Leroy",
         "Roux", "David", "Bertrand", "Morel", "Fournier", "Girard", "Bonnet",
         "Dupont", "Lambert", "Fontaine", "Rousseau", "Vincent", "Muller"],
    ),

    "UK": (
        ["Oliver", "Amelia", "George", "Isla", "Harry", "Poppy", "Jack", "Ava",
         "Charlie", "Isabella", "Thomas", "Sophie", "Oscar", "Emily", "William",
         "Alfie", "Lily", "Joshua", "Ella", "Ethan", "Grace", "Archie", "Freya",
         "Freddie", "Daisy", "Theo", "Phoebe", "Lucas", "Hannah", "Noah", "Ruby",
         "Henry", "Lucy", "Samuel", "Evie", "Edward", "Rosie", "Daniel", "Mia",
         "Logan", "Ellie", "James", "Imogen", "Alexander", "Alice", "Benjamin",
         "Jessica", "Max", "Chloe", "Joseph", "Florence", "Jake", "Molly",
         "Finley", "Harriet", "Toby", "Eva", "Reuben", "Martha", "Sebastian"],
        ["Smith", "Jones", "Williams", "Taylor", "Brown", "Davies", "Evans",
         "Wilson", "Thomas", "Roberts", "Johnson", "Lewis", "Walker", "Robinson",
         "Wood", "Thompson", "White", "Watson", "Jackson", "Wright", "Green",
         "Harris", "Cooper", "King", "Lee", "Martin", "Clarke", "James",
         "Morgan", "Hughes", "Edwards", "Hill", "Moore", "Clark", "Harrison",
         "Scott", "Young", "Morris", "Hall", "Ward", "Turner", "Carter",
         "Phillips", "Mitchell", "Patel", "Adams", "Campbell", "Anderson",
         "Allen", "Cook", "Bailey", "Bell", "Bennett", "Brooks", "Butler"],
    ),
    "GB": (
        ["Oliver", "Amelia", "George", "Isla", "Harry", "Poppy", "Jack", "Ava",
         "Charlie", "Isabella", "Thomas", "Sophie", "Oscar", "Emily", "William",
         "Alfie", "Lily", "Joshua", "Ella", "Ethan", "Grace", "Archie", "Freya"],
        ["Smith", "Jones", "Williams", "Taylor", "Brown", "Davies", "Evans",
         "Wilson", "Thomas", "Roberts", "Johnson", "Lewis", "Walker", "Robinson",
         "Wood", "Thompson", "White", "Watson", "Jackson", "Wright", "Green"],
    ),
    "US": (
        ["James", "Emma", "Liam", "Olivia", "Noah", "Ava", "William", "Sophia",
         "Benjamin", "Isabella", "Lucas", "Mia", "Henry", "Charlotte", "Mason",
         "Elijah", "Amelia", "Oliver", "Harper", "Logan", "Evelyn", "Ethan",
         "Abigail", "Aiden", "Emily", "Jackson", "Elizabeth", "Sebastian", "Sofia",
         "Carter", "Avery", "Owen", "Ella", "Wyatt", "Scarlett", "Dylan", "Grace",
         "Jack", "Chloe", "Grayson", "Victoria", "Levi", "Riley", "Isaac", "Aria",
         "Julian", "Lily", "Mateo", "Aubrey", "Ryan", "Zoey", "Nathan", "Penelope",
         "Aaron", "Lillian", "Luke", "Addison", "Hunter", "Layla", "Christian",
         "Natalie", "Josiah", "Camila", "Connor", "Hannah", "Landon", "Brooklyn",
         "Jonathan", "Zoe", "Nolan", "Stella", "Jeremiah", "Violet", "Eli", "Nora",
         "Caleb", "Aurora", "Isaiah", "Savannah", "Angel", "Audrey", "Andrew",
         "Claire", "Thomas", "Skylar", "Joshua", "Anna", "Ezra", "Paisley",
         "Hudson", "Ellie", "Xavier", "Samantha", "Jose", "Caroline", "Jayden"],
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
         "Davis", "Wilson", "Anderson", "Taylor", "Thomas", "Jackson", "White",
         "Harris", "Martin", "Thompson", "Young", "Robinson", "Lewis", "Walker",
         "Hall", "Allen", "King", "Wright", "Scott", "Green", "Baker", "Adams",
         "Nelson", "Hill", "Campbell", "Mitchell", "Roberts", "Carter", "Phillips",
         "Evans", "Turner", "Torres", "Parker", "Collins", "Edwards", "Stewart",
         "Flores", "Morris", "Nguyen", "Murphy", "Rivera", "Cook", "Rogers",
         "Morgan", "Peterson", "Cooper", "Reed", "Bailey", "Bell", "Gomez",
         "Kelly", "Howard", "Ward", "Cox", "Diaz", "Richardson", "Wood", "Watson",
         "Brooks", "Bennett", "Gray", "James", "Reyes", "Cruz", "Hughes", "Price",
         "Myers", "Long", "Foster", "Sanders", "Ross", "Morales", "Powell",
         "Sullivan", "Russell", "Ortiz", "Jenkins", "Gutierrez", "Perry", "Butler"],
    ),
    "JP": (
        ["Yuki", "Hana", "Ryo", "Saki", "Kento", "Aoi", "Sota", "Nana",
         "Haruto", "Yuna", "Ren", "Miku", "Kaito", "Rina", "Shota"],
        ["Sato", "Suzuki", "Takahashi", "Tanaka", "Watanabe", "Ito", "Yamamoto",
         "Nakamura", "Kobayashi", "Kato", "Yoshida", "Yamada", "Sasaki"],
    ),

    # 其他国家使用通用英文姓名
    "_default": (
        ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie", "Drew",
         "Blake", "Quinn", "Avery", "Peyton", "Reese", "Skyler", "Dakota"],
        ["Brown", "Smith", "Wilson", "Moore", "Anderson", "Jackson", "White",
         "Harris", "Martin", "Thompson", "Garcia", "Martinez", "Robinson"],
    ),
}


def get_names(country: str):
    """Return (first_names, last_names) for the given country code."""
    code = country.upper()
    return NAMES_BY_COUNTRY.get(code, NAMES_BY_COUNTRY["_default"])


def format_phone(raw: str, dial_code: str) -> str:
    """Convert raw number from API to E.164 format with the given country dial code."""
    raw = raw.strip()
    if raw.startswith(dial_code):
        return raw
    # Many APIs return numbers with a leading 0 (local format); strip it
    if raw.startswith("0"):
        raw = raw[1:]
    return f"{dial_code}{raw}"


def generate_username(country: str) -> str:
    first_names, last_names = get_names(country)
    first = random.choice(first_names)
    last = random.choice(last_names)
    # suffix = random.randint(1000, 9999)
    return f"{first}_{last}"


def generate_password() -> str:
    chars = (
        random.choices(string.ascii_uppercase, k=2) +
        random.choices(string.ascii_lowercase, k=5) +
        random.choices(string.digits, k=3) +
        random.choices("!@#$%^&*", k=2)
    )
    random.shuffle(chars)
    return "".join(chars)


class FirefoxSmsClient:
    def __init__(self, token: str, pid: int, api_country: str = None):
        self.token = token
        self.pid = pid
        self.api_country = api_country  # e.g. "usa" / "eng" / "fra"

    def get_phone(self) -> tuple:
        """Returns (raw_phone_number, pkey). Raises RuntimeError on failure."""
        params = {
            "act": "getPhone",
            "token": self.token,
            "iid": self.pid,
        }
        if self.api_country:
            params["country"] = self.api_country
        resp = requests.get(SMS_API_BASE, params=params, timeout=15)
        resp.raise_for_status()
        text = resp.text.strip()
        logger.debug(f"getPhone → {text}")
        if not text.startswith("1|"):
            raise RuntimeError(f"getPhone failed: {text}")
        # Response: 1|pkey|time|country|flag||tag|phone_number
        parts = text.split("|")
        if len(parts) < 8:
            raise RuntimeError(f"Unexpected getPhone format: {text}")
        pkey = parts[1]
        phone_raw = parts[7]
        return phone_raw, pkey

    def release(self, pkey: str) -> None:
        try:
            time.sleep(55)
            resp = requests.get(SMS_API_BASE, params={
                "act": "setRel",
                "token": self.token,
                "pkey": pkey,
            }, timeout=10)
            logger.debug(f"release({pkey[:8]}...) → {resp.text.strip()}")
        except Exception as e:
            logger.warning(f"Failed to release pkey={pkey}: {e}")


class AmazonPhoneChecker:
    def __init__(self, headless: bool = False):
        self.headless = headless
        self._pw = None
        self._browser = None
        self._page = None

    def start(self):
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = self._browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
            viewport={"width": 1280, "height": 800},
        )
        self._page = ctx.new_page()

    def stop(self):
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass

    def check(self, phone: str) -> str:
        """
        Returns:
          "new"      — phone not registered, can be used to create an account
          "existing" — phone already has an Amazon account
          "error"    — CAPTCHA, bot block, timeout, or unexpected page
        """
        page = self._page
        try:
            # Navigate to homepage first, then click the sign-in link naturally
            page.goto(AMAZON_HOME_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#nav-link-accountList a", timeout=15000)
            time.sleep(random.uniform(0.5, 1.2))
            page.click("#nav-link-accountList a")
            time.sleep(3)
            page.wait_for_selector("#ap_email_login", timeout=15000)
            time.sleep(random.uniform(0.8, 1.5))
            page.fill("#ap_email_login", phone)
            time.sleep(random.uniform(0.4, 0.9))
            page.click("#continue")

            try:
                page.wait_for_selector(
                    "#ap_password, #auth-error-message-box, #createAccountSubmit, "
                    "#ap_customer_name, #intention-submit-button, #intent-confirmation-form",
                    timeout=15000,
                )
            except PlaywrightTimeoutError:
                logger.warning(f"Timeout after Continue for {phone} | url={page.url}")
                return "error"

            # New number → "Proceed to create an account" confirmation form
            if page.query_selector("#intention-submit-button") or page.query_selector("#intent-confirmation-form"):
                logger.info(f"{phone} → new (create account confirmation page)")
                return "new"

            # Account exists → password field appeared
            if page.query_selector("#ap_password"):
                logger.info(f"{phone} → existing")
                return "existing"

            # # Registration form appeared directly (fallback)
            # if page.query_selector("#ap_customer_name") or page.query_selector("#createAccountSubmit"):
            #     logger.info(f"{phone} → new (registration form visible)")
            #     return "new"
            #
            # # Redirected to register/create page (fallback)
            # if "register" in page.url.lower() or "create" in page.url.lower():
            #     logger.info(f"{phone} → new (redirected to {page.url})")
            #     return "new"

            # Error message box — "cannot find account" means new (fallback)
            err_el = page.query_selector("#auth-error-message-box")
            if err_el:
                err_text = err_el.inner_text().lower()
                logger.debug(f"Auth error text: {err_text}")
                no_account_keywords = ("cannot find", "no account", "not found", "couldn't find")
                if any(kw in err_text for kw in no_account_keywords):
                    logger.info(f"{phone} → new (no account found message)")
                    return "new"

            logger.warning(f"Unexpected page state for {phone} | url={page.url}")
            return "error"

        except PlaywrightTimeoutError as e:
            logger.warning(f"Playwright timeout for {phone}: {e}")
            return "error"
        except Exception as e:
            logger.error(f"Error checking {phone}: {e}")
            return "error"


class ResultWriter:
    def __init__(self, path: str):
        self.path = Path(path)
        if not self.path.exists():
            with open(self.path, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["phone", "username", "password", "found_at"])

    def append(self, phone: str, username: str, password: str):
        with open(self.path, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow([phone, username, password, datetime.now().isoformat()])
        logger.success(f"Saved: {phone}  username={username}  password={password}")


def resolve_country_config(args) -> tuple:
    """Resolve (pid, dial_code, api_country, country_label) from CLI args."""
    country = (args.country or "").upper()
    if not country or country not in COUNTRY_CONFIG:
        supported = ", ".join(COUNTRY_CONFIG.keys())
        raise SystemExit(
            f"Error: --country '{country}' is not supported. "
            f"Supported: {supported}"
        )

    dial_code, builtin_pid, api_country = COUNTRY_CONFIG[country]
    pid: Optional[int] = args.pid if args.pid is not None else builtin_pid

    if pid is None:
        raise SystemExit(
            f"Error: --pid is required for country '{country}' (no built-in PID). "
            f"Check firefox.fun's price list for the project ID."
        )

    return pid, dial_code, api_country, country


def main():
    parser = argparse.ArgumentParser(
        description="Find registerable Amazon accounts using phone numbers from firefox.fun"
    )
    parser.add_argument("--count", type=int, default= 2,
                        help="Number of new accounts to find")
    parser.add_argument("--token", type=str, default=os.getenv("FIREFOX_SMS_TOKEN", ""),
                        help="firefox.fun API token")
    parser.add_argument("--country", type=str, default='UK',
                        help="Country code, e.g. FR / DE / UK / US / JP (uses built-in PID and dial code)")
    parser.add_argument("--pid", type=int, default=None,
                        help="firefox.fun project ID (overrides built-in, required if country has no built-in PID)")
    parser.add_argument("--output", type=str, default=None,
                        help="Output CSV file path (default: new_accounts_<COUNTRY>.csv)")
    parser.add_argument("--headless", action="store_true",
                        help="Run browser in headless mode")
    args = parser.parse_args()
    if not args.token:
        raise SystemExit("Error: --token is required or set FIREFOX_SMS_TOKEN")

    pid, dial_code, api_country, country_label = resolve_country_config(args)
    output = args.output or f"new_accounts_{country_label.lower()}.csv"

    sms = FirefoxSmsClient(token=args.token, pid=pid, api_country=api_country)
    checker = AmazonPhoneChecker(headless=args.headless)
    writer = ResultWriter(path=output)

    checker.start()
    logger.info(
        f"Country={country_label}  dial_code={dial_code}  pid={pid}  "
        f"target={args.count}  output={output}"
    )

    found = 0
    attempts = 0
    consecutive_errors = 0
    MAX_CONSECUTIVE_ERRORS = 10

    try:
        while found < args.count:
            time.sleep(3)
            attempts += 1
            phone_raw = pkey = None

            try:
                phone_raw, pkey = sms.get_phone()
            except Exception as e:
                logger.error(f"SMS platform error: {e}")
                time.sleep(5)
                continue

            phone = format_phone(phone_raw, dial_code)
            logger.info(f"[{found}/{args.count}] attempt={attempts}  checking {phone}")

            result = checker.check(phone)

            if result == "new":
                consecutive_errors = 0
                username = generate_username(country_label)
                password = generate_password()
                writer.append(phone, username, password)
                sms.release(pkey)
                found += 1
                time.sleep(random.uniform(1.5, 3.0))

            elif result == "existing":
                consecutive_errors = 0
                sms.release(pkey)
                time.sleep(random.uniform(2.0, 4.0))

            else:  # error
                consecutive_errors += 1
                sms.release(pkey)
                logger.warning(f"Consecutive errors: {consecutive_errors}/{MAX_CONSECUTIVE_ERRORS}")
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    logger.error("Too many consecutive errors — Amazon may be blocking. Stopping.")
                    break
                time.sleep(random.uniform(5.0, 10.0))

    finally:
        checker.stop()

    logger.info(f"Done. Found {found}/{args.count} in {attempts} attempts.")
    logger.info(f"Results saved to: {writer.path.resolve()}")


if __name__ == "__main__":
    main()
