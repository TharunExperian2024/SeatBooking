"""
Cloudbooking Automated Desk Booking Script
==========================================
Logs into experian.cloudbooking.com and books desks for your team.

Usage:
    # Batch mode (books for all team members using config.yml):
    python book_desk.py --config config.yml

    # Single booking (legacy CLI):
    python book_desk.py --email you@experian.com --date "16 Jun 2026" --contact "sandeep nuka"

    # Auto-scheduled via GitHub Actions (reads credentials from env vars)
    python book_desk.py --config config.yml
"""

import argparse
import getpass
import os
import re
import sys
from datetime import datetime, timedelta

import requests
import yaml
from bs4 import BeautifulSoup

BASE_URL = "https://experian.cloudbooking.com"
LOGIN_URL = f"{BASE_URL}/Login/Login.aspx"
FLOORPLAN_URL = f"{BASE_URL}/secure/bookings_floorplan.aspx?bt=2"

# Desk number to internal ID mapping
DESK_MAP = {
    "4293": "15360",
    "4294": "15361",
    "4295": "15362",
    "4296": "15363",
}

# Fallback defaults (used when no config file is provided)
PREFERRED_DESKS = ["4293", "4294", "4295", "4296"]
DEFAULTS = {
    "site_group": "11",
    "site": "55",
    "area": "131",
    "start_time": "07:00",
    "end_time": "23:00",
}


def load_config(config_path: str) -> dict:
    """Load booking configuration from YAML file."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def get_credentials(config: dict) -> tuple:
    """Get email/password from env vars (as specified in config) or prompt."""
    creds = config.get("credentials", {})
    email = os.environ.get(creds.get("email_env", "CB_EMAIL"), "")
    password = os.environ.get(creds.get("password_env", "CB_PASSWORD"), "")
    if not email:
        email = input("Email: ")
    if not password:
        password = getpass.getpass("Password: ")
    return email, password


def calculate_target_date(config: dict) -> str | None:
    """
    Calculate the target booking date based on schedule config.
    Returns date string like '17 Jun 2026' or None if today's target isn't a booking day.
    """
    schedule = config.get("schedule", {})
    days_ahead = schedule.get("days_ahead", 5)
    booking_days = schedule.get("booking_days", [2, 3, 4])  # Wed, Thu, Fri

    target = datetime.now() + timedelta(days=days_ahead)

    # Check if the target day is in our booking days list
    if target.weekday() in booking_days:
        return target.strftime("%d %b %Y")

    return None


def parse_hidden_fields(html: str) -> dict:
    """Extract ASP.NET hidden form fields from HTML."""
    soup = BeautifulSoup(html, "html.parser")
    fields = {}
    for inp in soup.find_all("input", attrs={"type": "hidden"}):
        name = inp.get("name")
        value = inp.get("value", "")
        if name:
            fields[name] = value
    return fields


def find_form_fields(html: str) -> dict:
    """Find login-specific input field names (email, password, button)."""
    # The login form uses: txtEmail, txtPassword, butLogin (type=button, not submit)
    return {
        "email_field": "txtEmail",
        "password_field": "txtPassword",
        "submit_field": "butLogin",
        "submit_value": "Log in",
    }


def login(session: requests.Session, email: str, password: str) -> bool:
    """
    Perform login to Cloudbooking.
    Returns True if login succeeded.
    """
    print("[*] Fetching login page...")
    resp = session.get(LOGIN_URL)
    resp.raise_for_status()

    # Parse hidden fields and form structure
    hidden = parse_hidden_fields(resp.text)
    form_info = find_form_fields(resp.text)

    email_field = form_info.get("email_field", "ctl00$txtEmail")
    password_field = form_info.get("password_field", "ctl00$txtPassword")
    submit_field = form_info.get("submit_field", "ctl00$butLogin")
    submit_value = form_info.get("submit_value", "Log in")

    print(f"[*] Found form fields: email={email_field}, password={password_field}, submit={submit_field}")

    # Build POST data
    data = dict(hidden)
    data[email_field] = email
    data[password_field] = password
    data[submit_field] = submit_value

    print("[*] Submitting login...")
    resp = session.post(LOGIN_URL, data=data, allow_redirects=True)
    resp.raise_for_status()

    # Check if login succeeded
    if "/Secure/" in resp.url or "Home.aspx" in resp.url or "Bookings" in resp.url:
        print(f"[+] Login successful! Redirected to: {resp.url}")
        return True

    # Check for error messages in the response
    if "incorrect" in resp.text.lower() or "invalid" in resp.text.lower():
        print("[-] Login failed: Invalid email or password.")
        return False

    # If still on login page
    if "Login.aspx" in resp.url:
        print("[-] Login failed: Still on login page.")
        # Try to extract error message
        soup = BeautifulSoup(resp.text, "html.parser")
        error = soup.find(attrs={"class": re.compile("error|alert|message", re.I)})
        if error:
            print(f"    Error: {error.get_text(strip=True)}")
        return False

    print(f"[?] Unclear result. Final URL: {resp.url}")
    return True


def get_day_name(date_str: str) -> str:
    """Get day name from date string like '16 Jun 2026'."""
    from datetime import datetime
    dt = datetime.strptime(date_str, "%d %b %Y")
    return dt.strftime("%A").upper()


def get_short_date(date_str: str) -> str:
    """Get 'JUN 16' format from '16 Jun 2026'."""
    from datetime import datetime
    dt = datetime.strptime(date_str, "%d %b %Y")
    return dt.strftime("%b %d").upper()


def scan_available_desks(date_response: str) -> dict:
    """
    Scan the floorplan AJAX response to find all desk spots and their status.
    Returns dict: {desk_number: {"id": internal_id, "status": "Green"|"Red"|"Blue", "title": ...}}
    """
    desks = {}
    # Pattern: imgbutSpot_XXXXX with title and src attributes
    spot_pattern = re.compile(
        r'imgbutSpot_(\d+)"[^>]*title="([^"]*)"[^>]*src="[^"]*?/(Green|Red|Blue)\.svg"'
    )
    for match in spot_pattern.finditer(date_response):
        internal_id = match.group(1)
        title = match.group(2)
        color = match.group(3)
        # Extract desk number from title (e.g. "4.293" or "4.293 : Someone")
        desk_num_match = re.match(r'(\d+)\.(\d+)', title)
        if desk_num_match:
            desk_number = desk_num_match.group(1) + desk_num_match.group(2)  # "4293"
        else:
            desk_number = internal_id  # fallback
        desks[desk_number] = {
            "id": internal_id,
            "status": color,
            "title": title,
        }
    return desks


def pick_best_desk(available_desks: dict, preferred: list = None, exclude_ids: set = None) -> tuple:
    """
    Pick the best available desk.
    Priority: preferred list order first, then nearest to preferred desks by number.
    exclude_ids: set of internal IDs already consumed by other team members this run.
    Returns (desk_number, internal_id, desk_label) or (None, None, None).
    """
    preferred = preferred or PREFERRED_DESKS
    exclude_ids = exclude_ids or set()

    # First: try preferred desks in order
    for desk_num in preferred:
        if desk_num in available_desks and available_desks[desk_num]["status"] == "Green":
            info = available_desks[desk_num]
            if info["id"] in exclude_ids:
                continue
            label = f"Floor 4 - 4.{desk_num[1:]}"
            print(f"[*] Preferred desk {desk_num} is available!")
            return desk_num, info["id"], label

    # Second: find nearest available desk to the preferred range
    preferred_nums = [int(d) for d in preferred]
    center = sum(preferred_nums) / len(preferred_nums)

    green_desks = [(num, info) for num, info in available_desks.items()
                   if info["status"] == "Green" and info["id"] not in exclude_ids]

    if not green_desks:
        return None, None, None

    # Sort by distance from center of preferred desks
    green_desks.sort(key=lambda x: abs(int(x[0]) - center))
    best_num, best_info = green_desks[0]
    floor_prefix = best_num[0]
    label = f"Floor {floor_prefix} - {floor_prefix}.{best_num[1:]}"
    print(f"[*] No preferred desk available. Nearest available: {best_num}")
    return best_num, best_info["id"], label


def book_desk(session: requests.Session, booking_date: str, desk_id: str = None,
              desk_label: str = None, contact_name: str = None,
              preferred_desks: list = None, auto_select: bool = False,
              exclude_ids: set = None, site_cfg: dict = None) -> tuple:
    """
    Book a desk after successful login.
    booking_date format: '16 Jun 2026'
    contact_name: None = book for self (no On Behalf Of), string = book on behalf of.
    exclude_ids: set of internal IDs already taken by other team members this run.
    Returns (success: bool, desk_id_used: str or None, error_reason: str or None).
      error_reason is "not_yet_open" when the date isn't bookable yet.
    """
    site = site_cfg or DEFAULTS
    preferred_desks = preferred_desks or PREFERRED_DESKS
    exclude_ids = exclude_ids or set()
    is_on_behalf_of = contact_name is not None

    # If no desk specified, use auto-select mode
    if not desk_id:
        auto_select = True
        # Use first preferred desk as initial target (will be overridden by scan)
        first_pref = preferred_desks[0] if preferred_desks else "4293"
        desk_id = DESK_MAP.get(first_pref, "15360")
        desk_label = f"Floor 4 - 4.{first_pref[1:]}"
    else:
        desk_label = desk_label or f"Floor 4 - 4.{desk_id}"

    print(f"\n[*] Navigating to floorplan page...")
    resp = session.get(FLOORPLAN_URL)
    resp.raise_for_status()

    if "Login" in resp.url:
        print("[-] Session expired or not logged in.")
        return (False, None, "session_expired")

    # Parse viewstate from the floorplan page
    hidden = parse_hidden_fields(resp.text)
    viewstate = hidden.get("__VIEWSTATE", "")
    viewstate_gen = hidden.get("__VIEWSTATEGENERATOR", "")

    if not viewstate:
        print("[-] Could not extract __VIEWSTATE from floorplan page.")
        return (False, None, "no_viewstate")

    print(f"[*] Got __VIEWSTATE ({len(viewstate)} chars)")

    day_name = get_day_name(booking_date)
    short_date = get_short_date(booking_date)
    floorplan_start = f"{booking_date} {DEFAULTS['start_time']}"
    floorplan_end = f"{booking_date} {DEFAULTS['end_time']}"

    print(f"[*] Target desk: {desk_label} (ID: {desk_id})")
    print(f"    Date: {day_name} {booking_date}")
    print(f"    Time: {DEFAULTS['start_time']} - {DEFAULTS['end_time']}")
    print(f"    On behalf of: {contact_name}")
    if auto_select:
        print(f"    Mode: AUTO-SELECT (preferred: {', '.join(preferred_desks)})")

    ajax_headers = {
        "X-MicrosoftAjax": "Delta=true",
        "X-Requested-With": "XMLHttpRequest",
    }

    # Step 1: Change the date on the floorplan to the booking date
    date_data = {
        "ctl00$ScriptManager1": "ctl00$ContentPlaceHolder1$upFloorPlan|ctl00$ContentPlaceHolder1$butDatePostBackDummy",
        "ctl00$cboSiteGroup": DEFAULTS["site_group"],
        "ctl00$cboSite": DEFAULTS["site"],
        "ctl00$cboLanguageSelector": "1",
        "ctl00$txtFeedback": "",
        "ctl00$ContentPlaceHolder1$txtDateDay": day_name,
        "ctl00$ContentPlaceHolder1$txtDate": short_date,
        "ctl00$ContentPlaceHolder1$txtDateDummy": short_date.title(),
        "ctl00$ContentPlaceHolder1$hiddenfieldSelectedDate": booking_date,
        "ctl00$ContentPlaceHolder1$hfldDisabledAccess": "False",
        "ctl00$ContentPlaceHolder1$hfldCustomHours": "False",
        "ctl00$ContentPlaceHolder1$hfldElectricCharging": "",
        "ctl00$ContentPlaceHolder1$cboAreas": DEFAULTS["area"],
        "ctl00$ContentPlaceHolder1$cboStartTime": floorplan_start,
        "ctl00$ContentPlaceHolder1$cboEndTime": floorplan_end,
        "ctl00$ContentPlaceHolder1$cboZoom": "100%",
        "ctl00$ContentPlaceHolder1$hiddenfieldPinListPosition": "0",
        "ctl00$ContentPlaceHolder1$hfInfoEntityID": "",
        "__EVENTTARGET": "ctl00$ContentPlaceHolder1$butDatePostBackDummy",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": viewstate_gen,
        "__ASYNCPOST": "true",
    }

    print(f"[*] Step 1: Changing floorplan date to {booking_date}...")
    resp = session.post(FLOORPLAN_URL, data=date_data, headers=ajax_headers)
    resp.raise_for_status()

    date_response = resp.text
    vs_match = re.search(r"__VIEWSTATE\|([^|]+)", date_response)
    vsg_match = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)", date_response)
    if vs_match:
        viewstate = vs_match.group(1)
        print(f"[*] Updated __VIEWSTATE after date change ({len(viewstate)} chars)")
    if vsg_match:
        viewstate_gen = vsg_match.group(1)

    # Scan all desks on the floor to determine availability
    all_desks = scan_available_desks(date_response)
    available_count = sum(1 for d in all_desks.values() if d["status"] == "Green")
    print(f"[*] Floor scan: {len(all_desks)} desks found, {available_count} available (Green)")

    if auto_select:
        # In auto-select mode, always pick the best available from preferred list
        best_num, best_id, best_label = pick_best_desk(all_desks, preferred_desks, exclude_ids)
        if best_id:
            desk_id = best_id
            desk_label = best_label
            print(f"[*] Auto-selected: {desk_label} (ID: {desk_id})")
        else:
            print("[-] No available desks found on this floor!")
            return (False, None, "no_desks")
    else:
        # Specific desk requested - check if it's available
        desk_spot_id = f"imgbutSpot_{desk_id}"
        spot_match = re.search(rf'{desk_spot_id}"[^>]*title="([^"]*)"[^>]*src="[^"]*?(/[^"]*)"', date_response)
        if spot_match:
            spot_title = spot_match.group(1)
            spot_src = spot_match.group(2)
            print(f"[*] Desk status on {booking_date}: title='{spot_title}', icon='{spot_src}'")
            if "Green" in spot_src:
                print("[*] Desk is AVAILABLE (green)")
            elif "Red" in spot_src or "Blue" in spot_src:
                color = "Red" if "Red" in spot_src else "Blue"
                print(f"[!] Desk {desk_label} is UNAVAILABLE ({color}) - {spot_title}")
                # Smart fallback: pick the best available
                best_num, best_id, best_label = pick_best_desk(all_desks, preferred_desks, exclude_ids)
                if best_id:
                    print(f"[*] Auto-switching to desk {best_label} (ID: {best_id})")
                    desk_id = best_id
                    desk_label = best_label
                else:
                    print("[-] No available desks found on this floor!")
                    return (False, None, "no_desks")
        else:
            print(f"[*] Could not find desk spot in date response (proceeding anyway)")

    # Save for debugging
    with open("date_response.txt", "w", encoding="utf-8") as f:
        f.write(date_response)

    # Step 2: Click on the desk (simulate image button click)
    select_data = {
        "ctl00$ScriptManager1": "ctl00$ContentPlaceHolder1$upFloorPlan|ctl00$ContentPlaceHolder1$imgbutSpot_" + desk_id,
        "ctl00$cboSiteGroup": DEFAULTS["site_group"],
        "ctl00$cboSite": DEFAULTS["site"],
        "ctl00$cboLanguageSelector": "1",
        "ctl00$txtFeedback": "",
        "ctl00$ContentPlaceHolder1$txtDateDay": day_name,
        "ctl00$ContentPlaceHolder1$txtDate": short_date,
        "ctl00$ContentPlaceHolder1$txtDateDummy": short_date.title(),
        "ctl00$ContentPlaceHolder1$hiddenfieldSelectedDate": booking_date,
        "ctl00$ContentPlaceHolder1$hfldDisabledAccess": "False",
        "ctl00$ContentPlaceHolder1$hfldCustomHours": "False",
        "ctl00$ContentPlaceHolder1$hfldElectricCharging": "",
        "ctl00$ContentPlaceHolder1$cboAreas": DEFAULTS["area"],
        "ctl00$ContentPlaceHolder1$cboStartTime": floorplan_start,
        "ctl00$ContentPlaceHolder1$cboEndTime": floorplan_end,
        "ctl00$ContentPlaceHolder1$cboZoom": "100%",
        "ctl00$ContentPlaceHolder1$hiddenfieldPinListPosition": "0",
        "ctl00$ContentPlaceHolder1$hfInfoEntityID": desk_id,
        f"ctl00$ContentPlaceHolder1$imgbutSpot_{desk_id}.x": "8",
        f"ctl00$ContentPlaceHolder1$imgbutSpot_{desk_id}.y": "8",
        "__EVENTTARGET": "",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": viewstate_gen,
        "__ASYNCPOST": "true",
    }

    print(f"[*] Step 2: Clicking desk {desk_id} on floorplan...")
    resp = session.post(FLOORPLAN_URL, data=select_data, headers=ajax_headers)
    resp.raise_for_status()

    select_response = resp.text
    vs_match = re.search(r"__VIEWSTATE\|([^|]+)", select_response)
    vsg_match = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)", select_response)
    if vs_match:
        viewstate = vs_match.group(1)
        print(f"[*] Updated __VIEWSTATE after desk click ({len(viewstate)} chars)")
    if vsg_match:
        viewstate_gen = vsg_match.group(1)

    # Check if desk booking form was populated
    if "hdnAddEditDeskDeskID" in select_response:
        deskid_match = re.search(r'hdnAddEditDeskDeskID"[^/]*value="([^"]*)"', select_response)
        if deskid_match and deskid_match.group(1):
            print(f"[*] Desk form populated with ID: {deskid_match.group(1)}")
        else:
            print("[!] Desk form hdnAddEditDeskDeskID is empty")

    with open("select_response.txt", "w", encoding="utf-8") as f:
        f.write(select_response)
    print(f"[DEBUG] Select response: {select_response[:300]}")

    # --- On Behalf Of flow (Steps 3-5) — only if booking for someone else ---
    search_name = ""
    contact_ctl = None
    if is_on_behalf_of:
        # Step 3: Click "On Behalf Of" button to activate contact search
        onbehalf_data = {
            "ctl00$ScriptManager1": "ctl00$upLookup|ctl00$butAddEditDeskBooking_OnBehalfOf",
            "ctl00$cboSiteGroup": site["site_group"],
            "ctl00$cboSite": site["site"],
            "ctl00$cboLanguageSelector": "1",
            "ctl00$txtFeedback": "",
            "ctl00$ContentPlaceHolder1$txtDateDay": day_name,
            "ctl00$ContentPlaceHolder1$txtDate": short_date,
            "ctl00$ContentPlaceHolder1$txtDateDummy": short_date.title(),
            "ctl00$ContentPlaceHolder1$hiddenfieldSelectedDate": booking_date,
            "ctl00$ContentPlaceHolder1$hfldDisabledAccess": "False",
            "ctl00$ContentPlaceHolder1$hfldCustomHours": "False",
            "ctl00$ContentPlaceHolder1$hfldElectricCharging": "",
            "ctl00$ContentPlaceHolder1$cboAreas": site["area"],
            "ctl00$ContentPlaceHolder1$cboStartTime": floorplan_start,
            "ctl00$ContentPlaceHolder1$cboEndTime": floorplan_end,
            "ctl00$ContentPlaceHolder1$cboZoom": "100%",
            "ctl00$ContentPlaceHolder1$hiddenfieldPinListPosition": "0",
            "ctl00$ContentPlaceHolder1$hfInfoEntityID": "",
            "ctl00$hdnAddEditDeskDeskID": desk_id,
            "ctl00$txtAddEditDeskBooking_Desk": desk_label,
            "ctl00$txtAddEditDeskBooking_DisplayName": "",
            "ctl00$txtAddEditDeskBooking_Start_Date": booking_date,
            "ctl00$cboAddEditDeskBooking_Start_Time": f"01 Jan 1900 {site['start_time']}",
            "ctl00$cboAddEditDeskBooking_End_Time": f"01 Jan 1900 {site['end_time']}",
            "ctl00$cboAddEditDeskBooking_BookingType": "0",
            "ctl00$txtAddEditDeskBooking_Notes": "",
            "ctl00$txtViewDeskBooking_BookingID": "",
            "ctl00$butAddEditDeskBooking_OnBehalfOf": "",
            "__EVENTTARGET": "",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": viewstate_gen,
            "__ASYNCPOST": "true",
        }

        print(f"[*] Step 3: Clicking 'On Behalf Of' button...")
        resp = session.post(FLOORPLAN_URL, data=onbehalf_data, headers=ajax_headers)
        resp.raise_for_status()

        onbehalf_response = resp.text
        vs_match = re.search(r"__VIEWSTATE\|([^|]+)", onbehalf_response)
        vsg_match = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)", onbehalf_response)
        if vs_match:
            viewstate = vs_match.group(1)
            print(f"[*] Updated __VIEWSTATE after OnBehalfOf click ({len(viewstate)} chars)")
        if vsg_match:
            viewstate_gen = vsg_match.group(1)

        # Step 4: Search for the contact
        search_name = contact_name.split()[0].lower()  # Use first name for search
        search_data = {
            "ctl00$ScriptManager1": "ctl00$upLookup|ctl00$butContactLookup_Search",
            "ctl00$cboSiteGroup": site["site_group"],
            "ctl00$cboSite": site["site"],
            "ctl00$cboLanguageSelector": "1",
            "ctl00$txtFeedback": "",
            "ctl00$ContentPlaceHolder1$txtDateDay": day_name,
            "ctl00$ContentPlaceHolder1$txtDate": short_date,
            "ctl00$ContentPlaceHolder1$txtDateDummy": short_date.title(),
            "ctl00$ContentPlaceHolder1$hiddenfieldSelectedDate": booking_date,
            "ctl00$ContentPlaceHolder1$hfldDisabledAccess": "False",
            "ctl00$ContentPlaceHolder1$hfldCustomHours": "False",
            "ctl00$ContentPlaceHolder1$hfldElectricCharging": "",
            "ctl00$ContentPlaceHolder1$cboAreas": site["area"],
            "ctl00$ContentPlaceHolder1$cboStartTime": floorplan_start,
            "ctl00$ContentPlaceHolder1$cboEndTime": floorplan_end,
            "ctl00$ContentPlaceHolder1$cboZoom": "100%",
            "ctl00$ContentPlaceHolder1$hiddenfieldPinListPosition": "0",
            "ctl00$ContentPlaceHolder1$hfInfoEntityID": "",
            "ctl00$txtContactLookup_Name": search_name,
            "ctl00$cboContactLookup_Mode": "1",
            "ctl00$hdnAddEditDeskDeskID": desk_id,
            "ctl00$txtAddEditDeskBooking_Desk": desk_label,
            "ctl00$txtAddEditDeskBooking_DisplayName": "",
            "ctl00$txtAddEditDeskBooking_Start_Date": booking_date,
            "ctl00$cboAddEditDeskBooking_Start_Time": f"01 Jan 1900 {site['start_time']}",
            "ctl00$cboAddEditDeskBooking_End_Time": f"01 Jan 1900 {site['end_time']}",
            "ctl00$cboAddEditDeskBooking_BookingType": "0",
            "ctl00$txtAddEditDeskBooking_Notes": "",
            "ctl00$txtViewDeskBooking_BookingID": "",
            "__EVENTTARGET": "ctl00$butContactLookup_Search",
            "__EVENTARGUMENT": "",
            "__VIEWSTATE": viewstate,
            "__VIEWSTATEGENERATOR": viewstate_gen,
            "__ASYNCPOST": "true",
        }

        print(f"[*] Step 4: Searching for contact '{search_name}'...")
        resp = session.post(FLOORPLAN_URL, data=search_data, headers=ajax_headers)
        resp.raise_for_status()

        contact_response = resp.text
        print(f"[DEBUG] Contact search response: {len(contact_response)} chars")

        vs_match = re.search(r"__VIEWSTATE\|([^|]+)", contact_response)
        vsg_match = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)", contact_response)
        if vs_match:
            viewstate = vs_match.group(1)
        if vsg_match:
            viewstate_gen = vsg_match.group(1)

        # Find the correct contact in the search results
        all_ctl_ids = list(dict.fromkeys(re.findall(r'repContactLookup\$(ctl\d+)\$chkContactLookup_Contact', contact_response)))
        display_names = re.findall(r'lblContactLookup_FullnameAndEmailAndCompany[^>]*>([^<]+)', contact_response)
        display_names = [n.strip().split(" - ")[0].strip() for n in display_names if n.strip()]
        print(f"[DEBUG] Found {len(all_ctl_ids)} ctl IDs: {all_ctl_ids}, display names: {display_names}")

        for i, dname in enumerate(display_names):
            if contact_name.lower().replace(" ", "") in dname.lower().replace(" ", "") or \
               dname.lower().replace(" ", "") in contact_name.lower().replace(" ", ""):
                if i < len(all_ctl_ids):
                    contact_ctl = all_ctl_ids[i]
                    print(f"[*] Found contact: '{dname}' at {contact_ctl}")
                    break

        if not contact_ctl:
            print(f"[-] Could not find contact '{contact_name}' in search results")
        else:
            # Step 5: Select the contact (check the checkbox)
            checkbox_field = f"ctl00$repContactLookup${contact_ctl}$chkContactLookup_Contact"
            select_contact_data = {
                "ctl00$ScriptManager1": f"ctl00$ScriptManager1|{checkbox_field}",
                "ctl00$cboSiteGroup": site["site_group"],
                "ctl00$cboSite": site["site"],
                "ctl00$cboLanguageSelector": "1",
                "ctl00$txtFeedback": "",
                "ctl00$ContentPlaceHolder1$txtDateDay": day_name,
                "ctl00$ContentPlaceHolder1$txtDate": short_date,
                "ctl00$ContentPlaceHolder1$txtDateDummy": short_date.title(),
                "ctl00$ContentPlaceHolder1$hiddenfieldSelectedDate": booking_date,
                "ctl00$ContentPlaceHolder1$hfldDisabledAccess": "False",
                "ctl00$ContentPlaceHolder1$hfldCustomHours": "False",
                "ctl00$ContentPlaceHolder1$hfldElectricCharging": "",
                "ctl00$ContentPlaceHolder1$cboAreas": site["area"],
                "ctl00$ContentPlaceHolder1$cboStartTime": floorplan_start,
                "ctl00$ContentPlaceHolder1$cboEndTime": floorplan_end,
                "ctl00$ContentPlaceHolder1$cboZoom": "100%",
                "ctl00$ContentPlaceHolder1$hiddenfieldPinListPosition": "0",
                "ctl00$ContentPlaceHolder1$hfInfoEntityID": "",
                "ctl00$txtContactLookup_Name": search_name,
                "ctl00$cboContactLookup_Mode": "1",
                checkbox_field: "on",
                "ctl00$hdnAddEditDeskDeskID": desk_id,
                "ctl00$txtAddEditDeskBooking_Desk": desk_label,
                "ctl00$txtAddEditDeskBooking_DisplayName": "",
                "ctl00$txtAddEditDeskBooking_Start_Date": booking_date,
                "ctl00$cboAddEditDeskBooking_Start_Time": f"01 Jan 1900 {site['start_time']}",
                "ctl00$cboAddEditDeskBooking_End_Time": f"01 Jan 1900 {site['end_time']}",
                "ctl00$cboAddEditDeskBooking_BookingType": "0",
                "ctl00$txtAddEditDeskBooking_Notes": "",
                "ctl00$txtViewDeskBooking_BookingID": "",
                "ctl00$chkRoomSelector_Site_Video": "on",
                "__EVENTTARGET": checkbox_field,
                "__EVENTARGUMENT": "",
                "__VIEWSTATE": viewstate,
                "__VIEWSTATEGENERATOR": viewstate_gen,
                "__ASYNCPOST": "true",
            }

            print(f"[*] Step 5: Selecting contact via {checkbox_field}...")
            resp = session.post(FLOORPLAN_URL, data=select_contact_data, headers=ajax_headers)
            resp.raise_for_status()

            contact_select_response = resp.text
            vs_match = re.search(r"__VIEWSTATE\|([^|]+)", contact_select_response)
            vsg_match = re.search(r"__VIEWSTATEGENERATOR\|([^|]+)", contact_select_response)
            if vs_match:
                viewstate = vs_match.group(1)
            if vsg_match:
                viewstate_gen = vsg_match.group(1)
    else:
        print(f"[*] Booking for SELF (skipping On Behalf Of steps)")

    # Step 6: Submit the booking
    save_data = {
        "ctl00$ScriptManager1": "ctl00$upAddEditDeskBooking|ctl00$butAddEditDeskBooking_Save",
        "ctl00$cboSiteGroup": site["site_group"],
        "ctl00$cboSite": site["site"],
        "ctl00$cboLanguageSelector": "1",
        "ctl00$txtFeedback": "",
        "ctl00$ContentPlaceHolder1$txtDateDay": day_name,
        "ctl00$ContentPlaceHolder1$txtDate": short_date,
        "ctl00$ContentPlaceHolder1$txtDateDummy": short_date.title(),
        "ctl00$ContentPlaceHolder1$hiddenfieldSelectedDate": "",
        "ctl00$ContentPlaceHolder1$hfldDisabledAccess": "False",
        "ctl00$ContentPlaceHolder1$hfldCustomHours": "False",
        "ctl00$ContentPlaceHolder1$hfldElectricCharging": "",
        "ctl00$ContentPlaceHolder1$cboAreas": site["area"],
        "ctl00$ContentPlaceHolder1$cboStartTime": floorplan_start,
        "ctl00$ContentPlaceHolder1$cboEndTime": floorplan_end,
        "ctl00$ContentPlaceHolder1$cboZoom": "100%",
        "ctl00$ContentPlaceHolder1$hiddenfieldPinListPosition": "0",
        "ctl00$ContentPlaceHolder1$hfInfoEntityID": "",
        "ctl00$hdnAddEditDeskDeskID": desk_id,
        "ctl00$txtAddEditDeskBooking_Desk": desk_label,
        "ctl00$txtAddEditDeskBooking_DisplayName": "",
        "ctl00$txtAddEditDeskBooking_Start_Date": booking_date,
        "ctl00$cboAddEditDeskBooking_Start_Time": f"01 Jan 1900 {site['start_time']}",
        "ctl00$cboAddEditDeskBooking_End_Time": f"01 Jan 1900 {site['end_time']}",
        "ctl00$cboAddEditDeskBooking_BookingType": "0",
        "ctl00$txtAddEditDeskBooking_Notes": "",
        "ctl00$txtViewDeskBooking_BookingID": "",
        "ctl00$chkRoomSelector_Site_Video": "on",
        "ctl00$cboRepeat_RepeatType": "1",
        "ctl00$cboRepeat_Every": "1",
        "ctl00$cboRepeat_MonthlyRepeatBy": "1",
        "ctl00$txtRepeat_StartDate": "",
        "ctl00$txtRepeat_EndDate": "",
        "ctl00$cboQuickBook_Room_AttendeeCount": "1",
        "ctl00$ucServiceSelector$cboAttendeeCount": "1",
        "ctl00$txtAddEditSpecialMenuItemQty": "0",
        "ctl00$cboAddEditVisitorBooking_VettingStatus": "-1",
        "ctl00$cboAddEditVisitorBooking_InductionStatus": "-1",
        "ctl00$cboAddEditAdvancedSearchBooking_ForTime": "30 m",
        "ctl00$cboRoomBookingAdvancedSearch_Capacity": "1",
        "ctl00$txtAvailabilityDateDay": "Monday",
        "ctl00$txtAvailabilityDate": "Jan 01",
        "ctl00$txtMultiAvailabilityDateDay": "Monday",
        "ctl00$txtMultiAvailabilityDate": "Jan 01",
        "__EVENTTARGET": "ctl00$butAddEditDeskBooking_Save",
        "__EVENTARGUMENT": "",
        "__VIEWSTATE": viewstate,
        "__VIEWSTATEGENERATOR": viewstate_gen,
        "__ASYNCPOST": "true",
    }

    # Add contact lookup fields only if On Behalf Of was used
    if is_on_behalf_of:
        save_data["ctl00$txtContactLookup_Name"] = search_name if contact_ctl else contact_name
        save_data["ctl00$cboContactLookup_Mode"] = "1"

    # Keep this minimal list; overly sparse payloads can fail unpredictably.
    empty_fields = [
        "ctl00$ContentPlaceHolder1$txtAddEditEntity_Name",
        "ctl00$ContentPlaceHolder1$txtAddEditEntity_Capacity",
        "ctl00$ContentPlaceHolder1$txtAddEditEntity_Order",
        "ctl00$ContentPlaceHolder1$txtAddEditEntity_Description",
        "ctl00$txtQuickBook_Room_MeetingTitle",
        "ctl00$txtQuickBook_Desk_Notes",
        "ctl00$txtAddEditRoomBookingID",
        "ctl00$txtAddEditParkingBookingID",
        "ctl00$txtAddEditPoolCarBookingID",
        "ctl00$txtAddEditResourceBookingID",
        "ctl00$txtAddEditHospitalityBookingID",
        "ctl00$txtAddEditDiningBookingID",
        "ctl00$txtAddEditPoolBikeBookingID",
        "ctl00$txtAddEditConferenceBookingID",
        "ctl00$txtAddEditMultiBookingID",
        "ctl00$txtAddEditVisitorBooking_Firstname",
        "ctl00$txtAddEditVisitorBooking_Surname",
        "ctl00$txtServiceNow_Description",
        "ctl00$txtServiceNow_Tel",
        "ctl00$txtAddEditSpecialMenuItem_Description",
    ]
    for field in empty_fields:
        save_data.setdefault(field, "")

    print("[*] Submitting booking request...")
    resp = session.post(FLOORPLAN_URL, data=save_data, headers=ajax_headers)
    resp.raise_for_status()

    response_text = resp.text

    with open("last_response.txt", "w", encoding="utf-8") as f:
        f.write(response_text)

    if "pageRedirect||%2fSecure%2fError" in response_text:
        print("[-] Booking FAILED: Server redirected to error page.")
        return (False, desk_id, "error")
    if "New Booking Created" in response_text or ("pageRedirect" in response_text and "Error" not in response_text):
        print(f"[+] Booking successful! Desk {desk_label}")
        return (True, desk_id, None)
    if "can not be booked" in response_text.lower():
        error_match = re.search(r"Desks can not be booked beyond (.+?)(?:\||$)", response_text)
        if error_match:
            print(f"[-] Date not open yet: Cannot book beyond {error_match.group(1)}")
        else:
            print("[-] Date not open yet.")
        return (False, desk_id, "not_yet_open")
    if "error" in response_text.lower():
        error_match = re.search(r"lblError[^>]*>([^<]+)", response_text)
        if error_match:
            print(f"[-] Booking failed: {error_match.group(1)}")
        else:
            print("[-] Booking failed: Error")
        return (False, desk_id, "error")

    if "upAddEditDeskBooking" in response_text and "hiddenField" in response_text:
        print(f"[+] Booking appears successful! Desk {desk_label}")
        return (True, desk_id, None)

    print("[?] Unclear response. Check bookings manually.")
    return (True, desk_id, None)


def book_all_team(session: requests.Session, config: dict, booking_date: str) -> int:
    """Book all configured team members for the target date."""
    site_cfg = config.get("site", DEFAULTS)
    schedule_cfg = config.get("schedule", {})
    site_cfg.setdefault("start_time", schedule_cfg.get("start_time", "07:00"))
    site_cfg.setdefault("end_time", schedule_cfg.get("end_time", "23:00"))
    preferred = config.get("preferred_desks", PREFERRED_DESKS)
    team = config.get("team", [])

    if not team:
        print("[-] No team members configured.")
        return 1

    consumed_ids = set()
    results = []

    print("\n" + "=" * 60)
    print(f"BATCH BOOKING: {booking_date}")
    print(f"Preferred desks: {', '.join(preferred)}")
    print("=" * 60)

    for member in team:
        name = member.get("name", "Unknown")
        contact = member.get("contact")

        print("\n" + "-" * 60)
        print(f"Booking for {name}" + (" (self)" if contact is None else f" (on behalf of: {contact})"))
        print("-" * 60)

        success, used_id, reason = book_desk(
            session,
            booking_date=booking_date,
            contact_name=contact,
            preferred_desks=preferred,
            auto_select=True,
            exclude_ids=consumed_ids,
            site_cfg=site_cfg,
        )

        if success and used_id:
            consumed_ids.add(used_id)
            desk_num = next((k for k, v in DESK_MAP.items() if v == used_id), used_id)
            desk_pretty = f"4.{desk_num[1:]}" if desk_num in DESK_MAP else used_id
            results.append((name, "Booked", desk_pretty))
            continue

        if reason == "not_yet_open":
            results.append((name, "Not yet open", "-"))
            break

        if reason == "no_desks":
            results.append((name, "No desks available", "-"))
        else:
            results.append((name, "Failed", "-"))

    print("\n" + "=" * 60)
    print(f"BOOKING SUMMARY - {booking_date}")
    print("=" * 60)
    for name, status, desk in results:
        print(f"{name:<24} {status:<18} {desk}")
    print("=" * 60 + "\n")

    if any(item[1] == "Not yet open" for item in results):
        print("[*] Date not open yet. Exiting 0 so schedule retries tomorrow.")
        return 0
    if any(item[1] in ("Failed", "No desks available") for item in results):
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="Automated Cloudbooking desk booking")
    parser.add_argument("--config", help="Path to YAML config for batch booking mode")
    parser.add_argument("--email", help="Login email address")
    parser.add_argument("--password", help="Login password (prompted if not provided)")
    parser.add_argument("--date", help="Booking date, e.g. '16 Jun 2026'")
    parser.add_argument("--desk-id", help="Desk internal ID (single-booking mode)")
    parser.add_argument("--desk-label", help="Desk label (single-booking mode)")
    parser.add_argument("--contact", help="On-behalf-of name (single-booking mode)")
    parser.add_argument("--login-only", action="store_true", help="Only test login")
    args = parser.parse_args()

    # Batch mode
    if args.config:
        config = load_config(args.config)
        email, password = get_credentials(config)

        booking_date = args.date
        if not booking_date:
            booking_date = calculate_target_date(config)
            if not booking_date:
                schedule = config.get("schedule", {})
                days_ahead = schedule.get("days_ahead", 5)
                target = datetime.now() + timedelta(days=days_ahead)
                print(f"[*] Target date {target.strftime('%d %b %Y')} ({target.strftime('%A')}) is outside configured booking days.")
                return 0

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

        if not login(session, email, password):
            return 1

        return book_all_team(session, config, booking_date)

    # Legacy single-booking mode
    email = args.email or input("Email: ")
    password = args.password or getpass.getpass("Password: ")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    if not login(session, email, password):
        return 1

    if args.login_only:
        print("\n[*] Login-only mode. Exiting.")
        return 0

    booking_date = args.date
    if not booking_date:
        booking_date = input("Booking date (e.g. '16 Jun 2026'): ")

    success, _, _ = book_desk(
        session,
        booking_date=booking_date,
        desk_id=args.desk_id,
        desk_label=args.desk_label,
        contact_name=args.contact,
    )
    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(main())
