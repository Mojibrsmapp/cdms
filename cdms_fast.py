"""
cdms_fast.py — browserless CDMS searcher via direct HTTP.

Flow per login session (one-time):
  GET  /f?p=105:101:0          → login page tokens
  POST /wwv_flow.accept         → login → get APEX session ID + cookies
  GET  /f?p=105:600:{sid}:::600:... → search page → scrape ck/protected (session-scoped)

Flow per search (reuses session):
  GET  /f?p=105:600:{sid}:::600:P600_NATIONALID,P600_DOB:{nid},{dob}
       → fresh p_page_submission_id + salt only (ck/protected already cached)
  POST /wwv_flow.accept         → returns JSON {"redirectURL": "..."}
  GET  {redirectURL}            → data HTML → parse fields
"""

import json
import os
import re
import threading
import time
from datetime import datetime
import requests
from bs4 import BeautifulSoup

CDMS_DOMAIN = "https://cdms.police.gov.bd"
CDMS_POOL   = f"{CDMS_DOMAIN}/cdms/cdms_pool"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

_AJAX_HEADERS = {
    "Accept":             "application/json, text/javascript, */*; q=0.01",
    "Accept-Language":    "en-US,en;q=0.9",
    "Origin":             CDMS_DOMAIN,
    "Sec-Fetch-Dest":     "empty",
    "Sec-Fetch-Mode":     "cors",
    "Sec-Fetch-Site":     "same-origin",
    "X-Requested-With":   "XMLHttpRequest",
    "sec-ch-ua":          '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
}

_GET_HEADERS = {
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language":           "en-US,en;q=0.9",
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "same-origin",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua":                 '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
    "sec-ch-ua-mobile":          "?0",
    "sec-ch-ua-platform":        '"Windows"',
}

DISABLED_CREDS_PATH  = "disabled_creds.json"
_DISABLE_TTL_HOURS   = 20
_disabled_lock       = threading.Lock()

_LIMIT_KEYWORDS   = ("সর্বোচ্চ সার্চের সীমা", "বার এনআইডি সার্চ করেছেন")
_LOCKED_KEYWORDS  = ("অস্থায়ীভাবে", "লক করা হয়েছে", "ভুল পাসওয়ার্ড")
_PROTECTED_FIELDS = ["P600_ERROR_MESSAGE", "P600_NEW_3", "P600_NEW_2"]

_APEX_ERRORS = {
    "invalid nid or date of birth":                             "invalid_nid_dob",
    "full authentication is required to access this resource":  "auth_required",
    "search permission without one of the mandatory fields":    "missing_fields",
}


_DBG_DIR = "debug_html"
os.makedirs(_DBG_DIR, exist_ok=True)


def _dbg_save(filename: str, html: str):
    path = os.path.join(_DBG_DIR, filename)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
        print(f"[dbg] saved {path}")
    except Exception as e:
        print(f"[dbg] could not save {path}: {e}")


def _load_disabled() -> set:
    """Return set of usernames still within TTL (not yet re-enabled)."""
    try:
        with open(DISABLED_CREDS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            data = {u: "2000-01-01T00:00:00" for u in data}
        if not isinstance(data, dict):
            return set()
        now = datetime.utcnow()
        active = set()
        for username, ts in data.items():
            try:
                age_hours = (now - datetime.fromisoformat(ts)).total_seconds() / 3600
                if age_hours < _DISABLE_TTL_HOURS:
                    active.add(username)
            except Exception:
                active.add(username)
        return active
    except Exception:
        return set()


def _save_disabled(username: str):
    with _disabled_lock:
        try:
            with open(DISABLED_CREDS_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                data = {u: "2000-01-01T00:00:00" for u in data}
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        data[username] = datetime.utcnow().isoformat()
        tmp = DISABLED_CREDS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DISABLED_CREDS_PATH)


def _get_apex_error(html: str) -> str | None:
    """Return error key if APEX error present in HTML, else None. Mirrors v5 get_apex_error."""
    def _match(txt):
        t = (txt or "").lower()
        for phrase, key in _APEX_ERRORS.items():
            if phrase in t:
                return key
        return None

    soup = BeautifulSoup(html, "lxml")

    el = soup.select_one("#t_Alert_Notification")
    if el:
        r = _match(el.get_text())
        if r:
            return r

    for el in soup.select(".htmldbStdErr"):
        r = _match(el.get_text())
        if r:
            return r

    for el in soup.select("[role='dialog'], .ui-dialog, .t-Dialog-body"):
        r = _match(el.get_text())
        if r:
            return r

    # Full page source fallback
    return _match(html)


# ─── HTML helpers ─────────────────────────────────────────────────────────────

def _inp(soup, id_=None, name=None) -> str:
    el = (soup.find("input", {"id": id_}) if id_ else None) or \
         (soup.find("input", {"name": name}) if name else None)
    return el.get("value", "") if el else ""


def _has_limit(text: str) -> bool:
    return any(k in text for k in _LIMIT_KEYWORDS)


def _scrape_page_tokens(html: str) -> dict:
    """Scrape all tokens from a page 600 GET response (per-load + session-scoped)."""
    soup = BeautifulSoup(html, "lxml")
    tokens = {
        "submission_id": _inp(soup, "pPageSubmissionId", "p_page_submission_id"),
        "salt":          _inp(soup, "pSalt",             "p_salt"),
        "P0_IP":         _inp(soup, "P0_IP",             "P0_IP"),
        "P0_G_IP":       _inp(soup, "P0_G_IP",           "P0_G_IP"),
        "protected":     _inp(soup, "pPageItemsProtected", "p_page_items_protected"),
    }
    for field in _PROTECTED_FIELDS:
        ck_el = soup.find("input", {"data-for": field})
        tokens[f"{field}_ck"] = ck_el.get("value", "") if ck_el else ""
    return tokens



def _get_field(soup, fid: str) -> str:
    el = soup.find(id=fid)
    if not el:
        return ""
    return (el.get("value") or el.get_text() or "").strip()


def _extract_photo(html: str) -> str:
    """HTML থেকে ফটোর সোর্স বের করে"""
    soup = BeautifulSoup(html, "lxml")
    # যে ইমেজে 'Photo' বা 'nid' আছে সেটা খুঁজে
    img = soup.find("img", src=re.compile(r"Photo|NID", re.I))
    if img:
        return img.get("src", "")
    
    # না পেলে কোন img এর সোর্স চেক করুন
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if "blob" in src or "data:image" in src or src.startswith("/"):
            return src
    return ""


# ─── Data extraction ──────────────────────────────────────────────────────────

def _extract_data(html: str, nid: str, dob: str) -> dict | None:
    soup = BeautifulSoup(html, "lxml")

    def g(fid):
        return _get_field(soup, fid)

    name_bangla = g("P600_PERSON_NAME")
    if not name_bangla:
        return None

    def addr(px):
        parts = [
            ("বাসা/হোল্ডিং",  g(f"P600_{px}HOMEORHOLDINGNO")),
            ("গ্রাম/রাস্তাঃ",  g(f"P600_{px}ADDI_VILL_OR_ROAD")),
            ("পোষ্ট অফিসঃ",   g(f"P600_{px}POSTOFFICE")),
            ("পোষ্ট কোডঃ",    g(f"P600_{px}POSTALCODE")),
            ("উপজেলাঃ",       g(f"P600_{px}UPOZILA")),
            ("জেলাঃ",          g(f"P600_{px}DISTRICT")),
            ("বিভাগঃ",         g(f"P600_{px}DIVISION")),
        ]
        return ", ".join(f"{k}: {v}" for k, v in parts if v)
    
    photo_src = _extract_photo(html)  # ← এখানে রাখুন

    return {
        "nid": nid, "dob": dob,
        "personal_info": {
            "name_bangla":   name_bangla,
            "name_english":  g("P600_PERSON_NAME_ENG"),
            "national_id":   g("P600_NID"),
            "gender":        g("P600_GEND"),
            "age":           g("P600_AGE"),
            "blood_group":   g("P600_BLOOD_GR"),
            "date_of_birth": g("P600_APP_BORNYEAR"),
            "father_name":   g("P600_FATH_NM"),
            "mother_name":   g("P600_MOTH_NM"),
        },
        "present_address": {
            "full_address": g("P600_PRESENT_ADDRESS"),
            "home_holding": g("P600_PHOMEORHOLDINGNO"),
            "village_road": g("P600_PADDI_VILL_OR_ROAD"),
            "post_office":  g("P600_PPOSTOFFICE"),
            "postal_code":  g("P600_PPOSTALCODE"),
            "upazila":      g("P600_PUPOZILA"),
            "district":     g("P600_PDISTRICT"),
            "division":     g("P600_PDIVISION"),
        },
        "permanent_address": {
            "full_address": g("P600_PERMANENT_ADDRESS"),
            "home_holding": g("P600_HOMEORHOLDINGNO"),
            "village_road": g("P600_ADDI_VILL_OR_ROAD"),
            "post_office":  g("P600_POSTOFFICE"),
            "postal_code":  g("P600_POSTALCODE"),
            "upazila":      g("P600_UPOZILA"),
            "district":     g("P600_DISTRICT"),
            "division":     g("P600_DIVISION"),
        },
        "perAddress": {"addressLine": addr("")},
        "preAddress":  {"addressLine": addr("P")},
        "photo": photo_src,
    }


# ─── FastSession ──────────────────────────────────────────────────────────────

class FastSession:
    def __init__(self, username: str, password: str):
        self.username     = username
        self.password     = password
        self.apex_session: str | None = None
        self.logged_in    = False
        self.limit_hit    = False
        self._lock        = threading.Lock()
        self._http        = requests.Session()
        self._http.headers.update({
            "User-Agent": UA,
            "Accept-Encoding": "gzip, deflate, br",
        })

    # ── login ─────────────────────────────────────────────────────────────────

    def login(self) -> bool:
        get_headers = {**_GET_HEADERS, "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1"}
        try:
            r = self._http.get(f"{CDMS_POOL}/f?p=105:101:0",
                               headers=get_headers, timeout=20, allow_redirects=True)
        except Exception as e:
            print(f"[fast] GET login failed [{self.username}]: {e}")
            return False

        soup = BeautifulSoup(r.text, "lxml")

        # submission_id has name attr only (no id)
        sub_id_el     = soup.find("input", {"name": "p_page_submission_id"})
        submission_id = sub_id_el.get("value", "") if sub_id_el else ""
        session_id    = _inp(soup, "pInstance", "p_instance")
        salt          = _inp(soup, "pSalt")          # id="pSalt", no name attr
        protected     = _inp(soup, "pPageItemsProtected")
        reload_sub    = _inp(soup, "pReloadOnSubmit") or "A"
        p0_ip         = _inp(soup, "P0_IP",   "P0_IP")
        p0_g_ip       = _inp(soup, "P0_G_IP", "P0_G_IP")

        # P101_TEXT is a server-protected field — APEX rejects login without its ck
        p101_text_val = _inp(soup, "P101_TEXT", "P101_TEXT")
        ck_el         = soup.find("input", {"data-for": "P101_TEXT"})
        p101_text_ck  = ck_el.get("value", "") if ck_el else ""

        print(f"[fast] Login [{self.username}]: sid={session_id} "
              f"sub={submission_id[:20]!r} salt={salt[:15]!r}")

        if not submission_id or not session_id:
            print(f"[fast] Missing tokens [{self.username}]")
            return False

        items = [
            {"n": "P0_IP",         "v": p0_ip},
            {"n": "P0_G_IP",       "v": p0_g_ip},
            {"n": "P0_CURR_URL",   "v": "CDMS"},
            {"n": "P101_USERNAME", "v": self.username},
            {"n": "P101_PASSWORD", "v": self.password},
        ]
        if p101_text_ck:
            items.append({"n": "P101_TEXT", "v": p101_text_val, "ck": p101_text_ck})

        # Device fingerprint fields — empty but present in form
        for f in ["P101_TOTAL_LOGIN_USER", "P101_CLINT_IP", "P101_OTP_FLAG",
                  "P101_PLATFORM", "P101_PLATFORM_VERSION", "P101_ARCHITECTURE",
                  "P101_MODEL", "P101_BROWSER_VERSION", "P101_USER_AGENT",
                  "P101_SCREEN_SIZE", "P101_PIXEL_RATIO", "P101_COLOR_DEPTH",
                  "P101_CPU_CORES", "P101_RAM_GB", "P101_TOUCH_POINTS",
                  "P101_TIMEZONE", "P101_WEBGL_VENDOR", "P101_WEBGL_RENDERER"]:
            items.append({"n": f, "v": ""})

        p_json = json.dumps({
            "pageItems": {
                "itemsToSubmit":       items,
                "protected":           protected,
                "rowVersion":          "",
                "formRegionChecksums": [],
            },
            "salt": salt,
        })

        body = {
            "p_flow_id":            "105",
            "p_flow_step_id":       "101",
            "p_instance":           session_id,
            "p_debug":              "",
            "p_request":            "LOGIN",
            "p_reload_on_submit":   reload_sub,
            "p_page_submission_id": submission_id,
            "p_json":               p_json,
        }

        try:
            r2 = self._http.post(
                f"{CDMS_POOL}/wwv_flow.accept",
                data=body,
                headers={
                    "Content-Type":              "application/x-www-form-urlencoded; charset=UTF-8",
                    "Referer":                   f"{CDMS_POOL}/f?p=105:101:{session_id}",
                    "Accept":                    "text/html,application/xhtml+xml,*/*;q=0.8",
                    "Accept-Language":           "en-US,en;q=0.9",
                    "Sec-Fetch-Dest":            "document",
                    "Sec-Fetch-Mode":            "navigate",
                    "Sec-Fetch-Site":            "same-origin",
                    "Sec-Fetch-User":            "?1",
                    "Upgrade-Insecure-Requests": "1",
                    "sec-ch-ua":                 '"Chromium";v="148", "Google Chrome";v="148", "Not/A)Brand";v="99"',
                    "sec-ch-ua-mobile":          "?0",
                    "sec-ch-ua-platform":        '"Windows"',
                },
                timeout=30,
                allow_redirects=True,
            )
        except Exception as e:
            print(f"[fast] POST login failed [{self.username}]: {e}")
            return False

        # Login success = redirected to page 600 (search page), NOT back to 101
        if "P101_USERNAME" in r2.text:
            if any(k in r2.text for k in _LOCKED_KEYWORDS):
                print(f"[fast] Account LOCKED [{self.username}] — skipping, will not retry")
                self.limit_hit = True
            else:
                print(f"[fast] Login failed — wrong password or unknown error [{self.username}] "
                      f"url={r2.url!r}")
            return False

        # Extract session ID — must come from page 600 (post-login destination)
        new_sid = None
        all_urls = [r2.url] + [h.url for h in r2.history]
        for url in all_urls:
            m = re.search(r"f\?p=\d+:600:(\d+)", url)   # page 600 only
            if m and m.group(1) not in ("0", ""):
                new_sid = m.group(1)
                break

        # Fallback: parse hidden p_instance from response HTML (post-login page)
        if not new_sid:
            soup2 = BeautifulSoup(r2.text, "lxml")
            new_sid = _inp(soup2, "pInstance", "p_instance") or None

        # Last resort: any f?p= URL in body
        if not new_sid:
            m = re.search(r"f\?p=\d+:600:(\d+)", r2.text)
            if m and m.group(1) not in ("0", ""):
                new_sid = m.group(1)

        if not new_sid:
            print(f"[fast] Login OK but no post-login session found [{self.username}] "
                  f"url={r2.url!r} body={r2.text[:300]!r}")
            return False

        self.apex_session = new_sid
        self.logged_in    = True
        print(f"[fast] Login OK: {self.username}  session={new_sid}")
        return True

    # ── search ────────────────────────────────────────────────────────────────

    def search(self, nid: str, dob: str) -> tuple:
        """
        Returns (data_dict | None, status_str).
        Status: 'success' | 'no_data' | 'limit' | 'invalid_nid_dob' |
                'session_expired' | 'nid_mismatch' | 'error'
        """
        sid = self.apex_session
        page_url = (f"{CDMS_POOL}/f?p=105:600:{sid}:::600:"
                    f"P600_NATIONALID,P600_DOB:{nid},{dob}")

        # ── 1. GET search page — only need fresh submission_id + salt ─────
        t0 = time.monotonic()
        print(f"[dbg] [{self.username}] step1: GET {page_url[:80]}")
        try:
            rg = self._http.get(
                page_url,
                headers={**_GET_HEADERS, "Referer": f"{CDMS_POOL}/f?p=105:600:{sid}"},
                timeout=15,
            )
        except Exception as e:
            print(f"[dbg] [{self.username}] step1 ERROR: {e}")
            return None, f"get_error:{e}"

        print(f"[dbg] [{self.username}] step1: status={rg.status_code} len={len(rg.text)}")

        if "P101_USERNAME" in rg.text:
            print(f"[dbg] [{self.username}] step1: session expired")
            _dbg_save(f"debug_s1_expired_{self.username}.html", rg.text)
            self.logged_in = False
            return None, "session_expired"

        if _has_limit(rg.text):
            print(f"[dbg] [{self.username}] step1: limit hit on GET")
            _dbg_save(f"debug_s1_limit_{self.username}.html", rg.text)
            self.limit_hit = True
            _save_disabled(self.username)
            return None, "limit"

        pg_tokens = _scrape_page_tokens(rg.text)
        print(f"[dbg] [{self.username}] step1: tokens — sub={bool(pg_tokens.get('submission_id'))} "
              f"salt={bool(pg_tokens.get('salt'))} protected={bool(pg_tokens.get('protected'))}")
        if not pg_tokens.get("submission_id"):
            _dbg_save(f"debug_s1_nosub_{self.username}.html", rg.text)
            print(f"[dbg] [{self.username}] step1: no submission_id — saved html")
            return None, "no_submission_id"

        # ── 2. Build POST body — all tokens from this page's GET ──────────
        items = [
            {"n": "P0_IP",           "v": pg_tokens.get("P0_IP", "")},
            {"n": "P0_G_IP",         "v": pg_tokens.get("P0_G_IP", "")},
            {"n": "P0_CURR_URL",     "v": "CDMS"},
            {"n": "P600_NATIONALID", "v": nid},
        ]
        for field in _PROTECTED_FIELDS:
            item: dict = {"n": field, "v": ""}
            ck = pg_tokens.get(f"{field}_ck", "")
            if ck:
                item["ck"] = ck
            items.append(item)
        items += [
            {"n": "P600_DOB",              "v": dob},
            {"n": "P600_PATH",             "v": ""},
            {"n": "P600_FIRSTLINEADDRESS", "v": ""},
        ]

        p_json = json.dumps({
            "pageItems": {
                "itemsToSubmit":       items,
                "protected":           pg_tokens.get("protected", ""),
                "rowVersion":          "",
                "formRegionChecksums": [],
            },
            "salt": pg_tokens["salt"],
        })

        body = {
            "p_flow_id":            "105",
            "p_flow_step_id":       "600",
            "p_instance":           sid,
            "p_debug":              "",
            "p_request":            "Search_In_NID",
            "p_reload_on_submit":   "S",
            "p_page_submission_id": pg_tokens["submission_id"],
            "p_json":               p_json,
        }

        # ── 3. POST search ────────────────────────────────────────────────
        print(f"[dbg] [{self.username}] step3: POST search NID={nid} DOB={dob}")
        try:
            rp = self._http.post(
                f"{CDMS_POOL}/wwv_flow.accept?p_context=105:600:{sid}",
                data=body,
                headers={**_AJAX_HEADERS,
                         "Referer": page_url,
                         "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
                timeout=30,
                allow_redirects=False,
            )
        except Exception as e:
            print(f"[dbg] [{self.username}] step3 ERROR: {e}")
            return None, f"post_error:{e}"

        print(f"[dbg] [{self.username}] step3: status={rp.status_code} "
              f"content-type={rp.headers.get('content-type','?')!r} body={rp.text[:200]!r}")
        if rp.status_code != 200:
            _dbg_save(f"debug_s3_post_{self.username}.html", rp.text)

        # ── 4. Parse POST JSON — check errors array, then follow redirectURL ─
        result_html = None
        try:
            payload = rp.json()

            # Check inline errors returned by CDMS before looking for redirect
            for err in payload.get("errors", []):
                msg = err.get("message", "")
                print(f"[dbg] [{self.username}] step4: inline error={msg.encode('unicode_escape').decode()!r}")
                if _has_limit(msg):
                    _dbg_save(f"debug_s4_limit_{self.username}.json", rp.text)
                    self.limit_hit = True
                    _save_disabled(self.username)
                    print(f"[fast] Limit [{self.username}] (inline JSON error)")
                    return None, "limit"
                apex_err = _get_apex_error(msg)
                if apex_err:
                    _dbg_save(f"debug_s4_apexerr_{self.username}.json", rp.text)
                    return None, apex_err
                # Any other inline error with non-empty message → invalid NID/DOB
                if msg.strip():
                    _dbg_save(f"debug_s4_err_{self.username}.json", rp.text)
                    print(f"[dbg] [{self.username}] step4: unknown inline error → invalid_nid_dob")
                    return None, "invalid_nid_dob"

            redirect = payload.get("redirectURL", "")
            print(f"[dbg] [{self.username}] step4: redirectURL={redirect!r}")
            if not redirect:
                _dbg_save(f"debug_s4_noredirect_{self.username}.json", rp.text)
            if redirect:
                if not redirect.startswith("http"):
                    redirect = f"{CDMS_POOL}/{redirect.lstrip('/')}"
                rr = self._http.get(
                    redirect,
                    headers={**_GET_HEADERS, "Referer": page_url},
                    timeout=20,
                )
                print(f"[dbg] [{self.username}] step4: redirect status={rr.status_code} len={len(rr.text)}")
                if rr.status_code != 200:
                    _dbg_save(f"debug_s4_redirect_{self.username}.html", rr.text)
                result_html = rr.text
        except Exception as e:
            print(f"[dbg] [{self.username}] step4: JSON parse failed ({e}) — using raw POST body")
            _dbg_save(f"debug_s4_jsonerr_{self.username}.html", rp.text)
            result_html = rp.text

        elapsed = round(time.monotonic() - t0, 2)

        if result_html is None:
            print(f"[dbg] [{self.username}] step4: no result_html")
            return None, "no_result_html"

        # ── 5. Check for errors in result page ────────────────────────────
        if _has_limit(result_html):
            _dbg_save(f"debug_s5_limit_{self.username}.html", result_html)
            self.limit_hit = True
            _save_disabled(self.username)
            print(f"[fast] Limit [{self.username}] in {elapsed}s")
            return None, "limit"

        apex_err = _get_apex_error(result_html)
        print(f"[dbg] [{self.username}] step5: apex_err={apex_err!r}")
        if apex_err == "invalid_nid_dob":
            _dbg_save(f"debug_s5_invalid_{self.username}.html", result_html)
            return None, "invalid_nid_dob"
        if apex_err in ("auth_required", "missing_fields"):
            _dbg_save(f"debug_s5_apexerr_{self.username}.html", result_html)
            return None, apex_err

        # ── 6. Extract data ───────────────────────────────────────────────
        data = _extract_data(result_html, nid, dob)
        print(f"[fast] [{self.username}] step6: NID={nid} → "
              f"{'success' if data else 'no_data'} in {elapsed}s")
        if data is None:
            _dbg_save(f"debug_s6_nodata_{self.username}.html", result_html)
            print(f"[dbg] [{self.username}] step6: no data extracted — saved html")

        if data is None:
            return {"status": "fail", "message": "No data found"}, "no_data"

        scraped_nid = data["personal_info"].get("national_id", "")
        if scraped_nid and scraped_nid != nid:
            print(f"[fast] NID mismatch: req={nid} got={scraped_nid}")
            return None, "nid_mismatch"

        return data, "success"


# ─── FastPool ─────────────────────────────────────────────────────────────────

class FastPool:
    def __init__(self, creds: list):
        self._sessions: list[FastSession] = []
        self._lock = threading.Lock()
        disabled = _load_disabled()
        for c in creds:
            if c.get("active", True):
                s = FastSession(c["username"], c["password"])
                if s.username in disabled:
                    s.limit_hit = True
                self._sessions.append(s)
        # Pre-login all non-disabled accounts in background threads
        self._prewarm()

    def _prewarm(self):
        """Login all non-disabled accounts concurrently in background threads."""
        def _login_one(sess):
            if sess.limit_hit:
                return
            with sess._lock:
                if not sess.logged_in:
                    sess.login()

        threads = [threading.Thread(target=_login_one, args=(s,), daemon=True)
                   for s in self._sessions]
        for t in threads:
            t.start()
        print(f"[fast] Prewarming {len(threads)} accounts in background...")

    def _reset_all(self):
        """All accounts exhausted — clear disabled file, reset limit flags only.
        Keep logged_in/apex_session so accounts don't need to re-login."""
        with _disabled_lock:
            with open(DISABLED_CREDS_PATH, "w", encoding="utf-8") as f:
                json.dump({}, f)
        with self._lock:
            for s in self._sessions:
                s.limit_hit = False   # reset limit only — keep session alive
        self._prewarm()               # re-login any that lost their session
        print("[fast] All accounts exhausted — disabled_creds.json cleared, limits reset")

    def search(self, nid: str, dob: str) -> tuple:
        tried = set()
        relogin_counts: dict = {}   # id(sess) → # of re-login attempts
        while True:
            with self._lock:
                sess = next(
                    (s for s in self._sessions
                     if not s.limit_hit and id(s) not in tried), None)
            if sess is None:
                self._reset_all()
                return None, "all_exhausted"

            with sess._lock:
                if not sess.logged_in:
                    ok = sess.login()
                    if not ok:
                        sess.limit_hit = True
                        tried.add(id(sess))
                        continue

                data, status = sess.search(nid, dob)

            if status == "success":
                return data, status
            if status == "no_data":
                return data, status
            if status == "invalid_nid_dob":
                return None, status
            if status == "limit":
                tried.add(id(sess))
                continue
            if status == "auth_required":
                key = id(sess)
                relogin_counts[key] = relogin_counts.get(key, 0) + 1
                if relogin_counts[key] >= 3:
                    tried.add(key)
                else:
                    with sess._lock:
                        sess.logged_in = False
                continue
            if status == "session_expired":
                key = id(sess)
                relogin_counts[key] = relogin_counts.get(key, 0) + 1
                if relogin_counts[key] >= 3:
                    tried.add(key)
                else:
                    with sess._lock:
                        sess.logged_in = False
                continue
            if status in ("nid_mismatch", "missing_fields"):
                tried.add(id(sess))
                continue

            tried.add(id(sess))
