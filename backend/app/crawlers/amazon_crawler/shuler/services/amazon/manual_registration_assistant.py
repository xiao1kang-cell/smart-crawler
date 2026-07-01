"""
Manual Amazon registration recorder.

This helper keeps the registration flow human-operated. It generates account
material, pauses at manual checkpoints, and records the finished account data
for later import/use by the crawler account tooling.
"""

import argparse
import csv
import re
import secrets
import string
import sys
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib.parse import parse_qs, urlparse


DEFAULT_OUTPUT = "manual_amazon_accounts.csv"
AMAZON_HOME_URL = "https://www.amazon.com/"
STATUS_VALUES = ("draft", "registered", "pending_verification", "failed")

CSV_FIELDS = [
    "username",
    "password",
    "totp_secret",
    "fingerprint_id",
    "proxy",
    "country",
    "display_name",
    "phone",
    "status",
    "created_at",
    "updated_at",
    "notes",
]

NAMES_BY_COUNTRY = {
    "HK": (
        ["Alex", "Ryan", "Jason", "Kevin", "Ethan", "Chloe", "Hannah", "Kelly", "Ivy", "Mandy"],
        ["Chan", "Lee", "Wong", "Cheung", "Leung", "Ho", "Lau", "Yuen", "Tsang", "Tang"],
    ),
    "US": (
        ["James", "Emma", "Liam", "Olivia", "Noah", "Ava", "William", "Sophia"],
        ["Smith", "Johnson", "Williams", "Brown", "Jones", "Miller", "Davis"],
    ),
    "UK": (
        ["Oliver", "Amelia", "George", "Isla", "Harry", "Poppy", "Jack", "Ava"],
        ["Smith", "Jones", "Williams", "Taylor", "Brown", "Davies", "Evans"],
    ),
    "_default": (
        ["Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie"],
        ["Brown", "Smith", "Wilson", "Moore", "Anderson", "Jackson", "White"],
    ),
}


@dataclass
class ManualAccountRecord:
    username: str
    password: str
    totp_secret: str = ""
    fingerprint_id: str = ""
    proxy: str = ""
    country: str = "US"
    display_name: str = ""
    phone: str = ""
    status: str = "registered"
    created_at: str = ""
    updated_at: str = ""
    notes: str = ""


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def generate_password(length: int = 16) -> str:
    if length < 12:
        raise ValueError("password length must be at least 12")

    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%^&*"),
    ]
    alphabet = string.ascii_letters + string.digits + "!@#$%^&*"
    required.extend(secrets.choice(alphabet) for _ in range(length - len(required)))
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


def generate_display_name(country: str) -> str:
    first_names, last_names = NAMES_BY_COUNTRY.get(country.upper(), NAMES_BY_COUNTRY["_default"])
    return f"{secrets.choice(first_names)} {secrets.choice(last_names)}"


def clean_totp_secret(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""

    if value.lower().startswith("otpauth://"):
        parsed = urlparse(value)
        secret_values = parse_qs(parsed.query).get("secret", [])
        value = secret_values[0] if secret_values else ""

    value = re.sub(r"[\s\-_]+", "", value).upper().rstrip("=")
    if value and not re.fullmatch(r"[A-Z2-7]+", value):
        raise ValueError("totp_secret must be a base32 secret or an otpauth:// URI")
    return value


def normalize_status(value: str) -> str:
    status = (value or "registered").strip().lower()
    if status not in STATUS_VALUES:
        raise ValueError(f"status must be one of: {', '.join(STATUS_VALUES)}")
    return status


def normalize_proxy(raw: str) -> str:
    value = (raw or "").strip()
    if not value:
        return ""

    if "://" in value:
        scheme, rest = value.split("://", 1)
        if scheme not in {"http", "https", "socks5"}:
            raise ValueError("proxy scheme must be http, https, or socks5")
        return f"{scheme}://{rest.strip()}"

    if "@" in value:
        return f"http://{value}"

    parts = value.split(":")
    if len(parts) == 2:
        host, port = parts
        if not host.strip() or not port.strip().isdigit():
            raise ValueError("proxy host:port format is invalid")
        return f"http://{host.strip()}:{port.strip()}"

    if len(parts) == 4:
        host, port, user, password = [part.strip() for part in parts]
        if not host or not port.isdigit() or not user or not password:
            raise ValueError("proxy host:port:user:pass format is invalid")
        return f"http://{user}:{password}@{host}:{port}"

    raise ValueError("proxy must be host:port, host:port:user:pass, or user:pass@host:port")


def load_proxy_pool(path: str) -> List[str]:
    if not path:
        return []
    proxy_path = Path(path)
    if not proxy_path.exists():
        raise ValueError(f"proxy file does not exist: {proxy_path}")

    proxies = []
    with proxy_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                proxies.append(normalize_proxy(raw))
            except ValueError as exc:
                raise ValueError(f"{proxy_path}:{line_no}: {exc}") from exc
    return proxies


def choose_proxy(args: argparse.Namespace) -> str:
    if args.proxy:
        return normalize_proxy(args.proxy)

    proxies = load_proxy_pool(args.proxy_file)
    if not proxies:
        return ""

    if args.proxy_strategy == "first":
        return proxies[0]
    return secrets.choice(proxies)


def read_records(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []

    with path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            rows.append({field: str(row.get(field, "") or "") for field in CSV_FIELDS})
        return rows


def write_records(path: Path, rows: List[Dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def upsert_record(path: Path, record: ManualAccountRecord) -> bool:
    rows = read_records(path)
    record_row = {field: str(asdict(record).get(field, "") or "") for field in CSV_FIELDS}
    key = record.username.strip()

    for idx, row in enumerate(rows):
        if row.get("username", "").strip() == key:
            if not record_row["created_at"]:
                record_row["created_at"] = row.get("created_at", "")
            rows[idx] = record_row
            write_records(path, rows)
            return False

    rows.append(record_row)
    write_records(path, rows)
    return True


def export_import_csv(source_path: Path, target_path: Path) -> None:
    rows = read_records(source_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        for row in rows:
            writer.writerow([row.get("username", ""), row.get("password", ""), row.get("totp_secret", "")])


def prompt(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if not value and default:
            value = default
        if value or not required:
            return value
        print(f"{label} is required.")


def checkpoint(message: str, use_popup: bool = False) -> None:
    print(f"\nACTION: {message}")
    if use_popup:
        try:
            import tkinter
            from tkinter import messagebox

            root = tkinter.Tk()
            root.withdraw()
            messagebox.showinfo("Manual checkpoint", message)
            root.destroy()
        except Exception as exc:
            print(f"Popup unavailable: {exc}")
    input("Press Enter after this manual step is complete...")


def build_record_from_args(args: argparse.Namespace) -> ManualAccountRecord:
    country = (args.country or "US").strip().upper()
    phone = (args.phone or "").strip()
    username = (args.username or phone).strip()
    if not username:
        raise ValueError("--username or --phone is required unless --interactive is used")

    password = (args.password or "").strip() or generate_password(args.password_length)
    proxy = choose_proxy(args)
    status = normalize_status(args.status)
    created_at = args.created_at or now_iso()

    return ManualAccountRecord(
        username=username,
        password=password,
        totp_secret=clean_totp_secret(args.totp_secret or ""),
        fingerprint_id=(args.fingerprint_id or "").strip(),
        proxy=proxy,
        country=country,
        display_name=(args.display_name or generate_display_name(country)).strip(),
        phone=phone,
        status=status,
        created_at=created_at,
        updated_at=now_iso(),
        notes=(args.notes or "").strip(),
    )


def build_record_interactive(args: argparse.Namespace) -> ManualAccountRecord:
    print("Manual Amazon registration assistant")
    print("All Amazon, SMS, CAPTCHA, and 2FA steps must be completed manually.")

    country = prompt("Country code", (args.country or "HK").upper(), required=True).upper()
    proxy = prompt("Proxy for the fingerprint profile", choose_proxy(args), required=False)
    fingerprint_id = prompt("Fingerprint browser codeID", args.fingerprint_id or "", required=True)
    phone = prompt("Phone/account", args.phone or "", required=True)
    username = prompt("Saved username", args.username or phone, required=True)
    display_name = prompt("Display name", args.display_name or generate_display_name(country), required=True)
    password = prompt("Password", args.password or generate_password(args.password_length), required=True)

    print("\nUse these values during your manual registration:")
    print(f"  account      : {username}")
    print(f"  display name : {display_name}")
    print(f"  password     : {password}")
    print(f"  profile code : {fingerprint_id}")
    print(f"  proxy        : {proxy}")

    if args.open_url:
        webbrowser.open(args.amazon_url)

    checkpoint(
        "Open the selected browser profile and complete Amazon registration manually. "
        "Handle any CAPTCHA/SMS verification yourself.",
        use_popup=args.popup,
    )
    checkpoint(
        "Enable 2-step verification manually, then copy the TOTP secret or otpauth URI.",
        use_popup=args.popup,
    )

    totp_secret = clean_totp_secret(prompt("TOTP secret / otpauth URI", args.totp_secret or "", required=False))
    status = normalize_status(prompt("Status", args.status or "registered", required=True))
    notes = prompt("Notes", args.notes or "", required=False)
    created_at = args.created_at or now_iso()

    return ManualAccountRecord(
        username=username,
        password=password,
        totp_secret=totp_secret,
        fingerprint_id=fingerprint_id,
        proxy=proxy,
        country=country,
        display_name=display_name,
        phone=phone,
        status=status,
        created_at=created_at,
        updated_at=now_iso(),
        notes=notes,
    )


def parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Record manually registered Amazon accounts")
    parser.add_argument("--interactive", action="store_true", help="Prompt through a manual registration session")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"Full CSV output path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--import-csv-output", default="", help="Optional 3-column username/password/totp_secret CSV export")
    parser.add_argument("--country", default="HK", help="Country/site label to save, e.g. HK/US/UK")
    parser.add_argument("--username", default="", help="Saved account username/login")
    parser.add_argument("--phone", default="", help="Phone number used for the account")
    parser.add_argument("--display-name", default="", help="Amazon display name to save")
    parser.add_argument("--password", default="", help="Password to save; generated if omitted")
    parser.add_argument("--password-length", type=int, default=16, help="Generated password length")
    parser.add_argument("--fingerprint-id", default="", help="Manually created fingerprint browser codeID/profile ID")
    parser.add_argument("--proxy", default="", help="Proxy to save/use, e.g. host:port:user:pass")
    parser.add_argument("--proxy-file", default="", help="Proxy pool file, one proxy per line")
    parser.add_argument("--proxy-strategy", choices=("random", "first"), default="random", help="Proxy selection strategy")
    parser.add_argument("--totp-secret", default="", help="2FA TOTP base32 secret or otpauth:// URI")
    parser.add_argument("--status", default="registered", help=f"One of: {', '.join(STATUS_VALUES)}")
    parser.add_argument("--notes", default="", help="Optional note saved with the row")
    parser.add_argument("--created-at", default="", help="Override created_at timestamp")
    parser.add_argument("--open-url", action="store_true", help="Open Amazon URL in the default browser")
    parser.add_argument("--amazon-url", default=AMAZON_HOME_URL, help="URL used with --open-url")
    parser.add_argument("--popup", action="store_true", help="Show local popup checkpoints in interactive mode")
    parser.add_argument("--dry-run", action="store_true", help="Print the record without saving")
    return parser.parse_args(argv)


def main(argv: List[str] = None) -> int:
    args = parse_args(argv or sys.argv[1:])

    try:
        record = build_record_interactive(args) if args.interactive else build_record_from_args(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        for field in CSV_FIELDS:
            print(f"{field}: {getattr(record, field)}")
        return 0

    output_path = Path(args.output)
    inserted = upsert_record(output_path, record)
    action = "inserted" if inserted else "updated"
    print(f"{action}: {record.username}")
    print(f"saved: {output_path.resolve()}")

    if args.import_csv_output:
        import_path = Path(args.import_csv_output)
        export_import_csv(output_path, import_path)
        print(f"import csv saved: {import_path.resolve()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
