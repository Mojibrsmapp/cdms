import sys
import threading
import queue as _q
import json
import base64
import requests
import os
import random
import re
import concurrent.futures
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
import time
from datetime import datetime

# ─── Config ─────────────────────────────────────────────────────────────────
MAX_QUEUE_SIZE       = 50  # max API requests waiting in queue before rejection
HEADLESS            = True
BROWSERS_PER_ACCOUNT = 1    # 💡 ১০ থেকে কমিয়ে ১ করুন (একসাথে বেশি ব্রাউজার ওপেন হবে না)
PREWARM_COUNT        = 0    # 💡 এটি ০ করে দিন, শুরুতে সব আইডি একসাথে লগইন করার চেষ্টা করবে না
MAX_BROWSERS         = 2    # 💡 ক্যাপ কমিয়ে দিন যাতে সার্ভারের ওপর চাপ না পড়ে


CACHE_DIR           = "cache"
UPLOAD_DIR          = "uploads"
DISABLED_FILE       = "disabled_creds.json"
SUCCESS_COUNTS_FILE = "success_counts.json"

TEST_NID = "6932271742"
TEST_DOB = "2005-01-01"

for d in (CACHE_DIR, UPLOAD_DIR):
    os.makedirs(d, exist_ok=True)

_success_lock = threading.Lock()


def _load_success_counts():
    try:
        with open(SUCCESS_COUNTS_FILE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: int(v) for k, v in data.items()}
    except FileNotFoundError:
        pass
    except Exception as e:
        safe_print(f"Warning: could not load {SUCCESS_COUNTS_FILE}: {e}")
    return {}


def _save_success_counts(counts):
    tmp = SUCCESS_COUNTS_FILE + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(counts, f, indent=2)
        os.replace(tmp, SUCCESS_COUNTS_FILE)
    except Exception as e:
        safe_print(f"Warning: could not save {SUCCESS_COUNTS_FILE}: {e}")


_cred_success_counts = _load_success_counts()

_UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
       'AppleWebKit/537.36 (KHTML, like Gecko) '
       'Chrome/120.0.0.0 Safari/537.36')

_CDMS_DOMAIN = "https://cdms.police.gov.bd"
_CDMS_BASE   = f"{_CDMS_DOMAIN}/cdms/cdms_pool/f?p=105"


def safe_print(text):
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode('utf-8', 'ignore').decode('utf-8'))


# ─── Credentials ────────────────────────────────────────────────────────────

def get_credentials_list():
    try:
        with open("accounts.json", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []



# ─── Result cache (lifetime — same NID+DOB always returns same data) ────────

def _cache_path(nid, dob):
    return os.path.join(CACHE_DIR, f"{nid}_{dob}.json")


def cache_get(nid, dob):
    try:
        with open(_cache_path(nid, dob), encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:
        safe_print(f"Cache read error [{nid}]: {e}")
        return None


def cache_set(nid, dob, data):
    path = _cache_path(nid, dob)
    tmp  = path + ".tmp"
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        safe_print(f"Cache write error [{nid}]: {e}")


# ─── Driver factory ──────────────────────────────────────────────────────────

def create_driver():
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    
    opts.add_argument('--headless=new') 
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1280,720')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    
    # 💡 এই লাইনটি যোগ করুন: Tor প্রক্সি লোকালহোস্টে কানেক্ট করা
    opts.add_argument('--proxy-server=socks5://127.0.0.1:9050')
    
    opts.binary_location = "/usr/bin/chromium"
    
    # বাকি কোড আগের মতোই থাকবে...
    from selenium.webdriver.chrome.service import Service
    service = Service(executable_path="/usr/bin/chromium-driver")
    driver = webdriver.Chrome(service=service, options=opts)
    return driver


# ─── Page state checkers ────────────────────────────────────────────────────

def _src(driver):
    try:
        return driver.page_source
    except Exception:
        return ""


def on_login_page(driver):
    return "P101_USERNAME" in _src(driver)


def on_data_page(driver):
    s = _src(driver)
    return "P600_NATIONALID" in s and "B635854564534766524" in s


def check_data_loaded(driver):
    # Only check P600_PERSON_NAME — it's never set from URL params or session
    # state residue (unlike P600_NID which can be pre-filled), so it only
    # has a value after a real successful AJAX search completes.
    try:
        el = driver.find_element(By.ID, "P600_PERSON_NAME")
        v = (el.get_attribute("value") or el.text or "").strip()
        return bool(v)
    except Exception:
        return False


def check_password_change_page(driver):
    try:
        t = _src(driver).lower()
        return any(k in t for k in [
            "password change", "change password", "get email id from fims",
            "enter otp", "old password", "new password", "confirm new password",
            "পাসওয়ার্ড পরিবর্তন",
        ])
    except Exception:
        return False


_LIMIT_KEYWORDS = ("এনআইডি সার্চ", "সর্বোচ্চ সার্চের সীমা", "বার এনআইডি সার্চ করেছেন")


def _has_limit_text(txt):
    return any(k in txt for k in _LIMIT_KEYWORDS)


def check_search_limit_error(driver):
    # 1. Inline page-level APEX notification banner
    try:
        el = driver.find_element(By.ID, "t_Alert_Notification")
        if _has_limit_text(el.get_attribute("textContent") or ""):
            return True
    except Exception:
        pass
    # 2. Individual error list items (APEX dynamic injection)
    try:
        for item in driver.find_elements(By.CSS_SELECTOR, ".htmldbStdErr"):
            if _has_limit_text(item.get_attribute("textContent") or ""):
                return True
    except Exception:
        pass
    # 3. APEX/jQuery-UI modal dialogs (role=dialog, .ui-dialog, .t-Dialog)
    try:
        for dlg in driver.find_elements(By.CSS_SELECTOR,
                "[role='dialog'], .ui-dialog, .t-Dialog-body"):
            txt = dlg.get_attribute("textContent") or ""
            if _has_limit_text(txt):
                return True
    except Exception:
        pass
    # 4. Page source fallback (catches text not yet in live DOM)
    try:
        s = _src(driver)
        return ("আপনি আজকে" in s and "সর্বোচ্চ সার্চের সীমা" in s)
    except Exception:
        return False


_APEX_ERRORS = {
    "invalid nid or date of birth":                                   "invalid_nid_dob",
    "full authentication is required to access this resource":        "auth_required",
    "search permission without one of the mandatory fields":          "missing_fields",
}


def get_apex_error(driver):
    """Return error key if an APEX error dialog/banner is present, else None."""
    def _match(txt):
        t = (txt or "").lower()
        for phrase, key in _APEX_ERRORS.items():
            if phrase in t:
                return key
        return None

    for sel in ["#t_Alert_Notification"]:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            r = _match(el.get_attribute("textContent"))
            if r:
                return r
        except Exception:
            pass
    try:
        for item in driver.find_elements(By.CSS_SELECTOR, ".htmldbStdErr"):
            r = _match(item.get_attribute("textContent"))
            if r:
                return r
    except Exception:
        pass
    try:
        for dlg in driver.find_elements(By.CSS_SELECTOR,
                "[role='dialog'], .ui-dialog, .t-Dialog-body"):
            r = _match(dlg.get_attribute("textContent"))
            if r:
                return r
    except Exception:
        pass
    try:
        r = _match(driver.page_source)
        if r:
            return r
    except Exception:
        pass
    return None


def check_automation_blocked(driver):
    try:
        t = _src(driver).lower()
        return any(k in t for k in ["automated test", "controlled by automated"])
    except Exception:
        return False


def check_stuck_page(driver):
    try:
        s = _src(driver)
        if "t-NavigationBar" in s and "t-Button-label" in s:
            return not check_password_change_page(driver) and not on_data_page(driver)
        return False
    except Exception:
        return False


# ─── Login ───────────────────────────────────────────────────────────────────

def _type_field(element, text):
    """Type char-by-char with small random delay — bypasses paste-disabled sites."""
    element.clear()
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.06, 0.12))


def handle_password_change(driver):
    for attempt in [
        lambda: driver.back(),
        lambda: driver.find_element(By.CSS_SELECTOR, "input[value='Cancel']").click(),
        lambda: driver.find_element(By.CSS_SELECTOR, "button[type='button']").click(),
        lambda: driver.find_element(By.XPATH, "//button[contains(text(),'Cancel')]").click(),
        lambda: driver.find_element(By.XPATH, "//a[contains(text(),'Skip')]").click(),
    ]:
        try:
            attempt()
            time.sleep(0.3)
            return True
        except Exception:
            continue
    return False


def perform_login(driver, username, password):
    """Full login flow. Returns (success: bool, status: str)."""
    try:
        user_field = WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.ID, "P101_USERNAME"))
        )
        safe_print(f"Typing username: {username}")
        _type_field(user_field, username)

        pwd_field = driver.find_element(By.ID, "P101_PASSWORD")
        safe_print("Typing password...")
        _type_field(pwd_field, password)

        driver.execute_script("arguments[0].click();",
                              driver.find_element(By.ID, "P101_LOGIN"))
        safe_print("Login clicked, waiting...")

        try:
            WebDriverWait(driver, 20).until(lambda d: (
                on_data_page(d) or
                check_password_change_page(d) or
                check_stuck_page(d) or
                check_automation_blocked(d) or
                not on_login_page(d)
            ))
        except TimeoutException:
            pass

        if check_automation_blocked(driver):
            return False, "automation_blocked"
        if check_password_change_page(driver):
            if not handle_password_change(driver):
                return False, "password_change_required"
        if check_stuck_page(driver):
            return False, "stuck_page"
        if on_data_page(driver):
            return True, "success"
        if on_login_page(driver):
            return False, "login_failed"
        return True, "unknown_page"
    except Exception as e:
        return False, str(e)


# ─── Session helpers ──────────────────────────────────────────────────────────

def _get_session_id(driver):
    """Extract the real APEX session ID from the current URL."""
    try:
        m = re.search(r'f\?p=\d+:\d+:(\d+)', driver.current_url)
        if m and m.group(1) not in ('0', ''):
            return m.group(1)
    except Exception:
        pass
    return None


def _nid_url(nid, dob, session='115196342139283'):
    return (f"{_CDMS_BASE}:600:{session}:::600:"
            f"P600_NATIONALID,P600_DOB:{nid},{dob}")


def _wait_for_page(driver, timeout=12):
    """Wait until on login page or data page."""
    try:
        WebDriverWait(driver, timeout).until(
            lambda d: on_login_page(d) or on_data_page(d)
        )
    except TimeoutException:
        pass


def do_login(driver, username, password):
    """Navigate to login page and login. Returns (ok, status)."""
    try:
        driver.get(f"{_CDMS_DOMAIN}/cdms/cdms_pool/")
        driver.delete_all_cookies()
    except Exception:
        pass
    driver.get(f"{_CDMS_BASE}:600:115196342139283")
    _wait_for_page(driver, timeout=12)

    if not on_login_page(driver):
        safe_print(f"Already logged in: {username}")
        return True, "already_logged_in"

    ok, status = perform_login(driver, username, password)
    if ok:
        safe_print(f"Login success: {username}")
    else:
        safe_print(f"Login failed: {username} — {status}")
    return ok, status


# ─── Search ──────────────────────────────────────────────────────────────────

def click_search_and_wait(driver):
    try:
        btn = WebDriverWait(driver, 15).until(
            EC.element_to_be_clickable((By.ID, "B635854564534766524"))
        )
        # Clear all result fields — ClearCache=600 flushes APEX session server-side
        # but DOM may still carry rendered values; wipe them before clicking
        driver.execute_script("""
            ['P600_PERSON_NAME','P600_NID','P600_FATH_NM','P600_PERSON_NAME_ENG',
             'P600_GEND','P600_AGE','P600_BLOOD_GR','P600_APP_BORNYEAR',
             'P600_MOTH_NM','P600_PRESENT_ADDRESS','P600_PERMANENT_ADDRESS'].forEach(function(id){
                var el = document.getElementById(id);
                if (el) el.value = '';
            });
        """)
        driver.execute_script("arguments[0].click();", btn)
        try:
            WebDriverWait(driver, 60).until(
                lambda d: check_search_limit_error(d) or check_data_loaded(d) or get_apex_error(d)
            )
        except TimeoutException:
            pass
        if check_search_limit_error(driver):
            return False, "search_limit_exceeded"
        apex_err = get_apex_error(driver)
        if apex_err:
            return False, apex_err
        if check_data_loaded(driver):
            # Data appeared — confirm no limit banner is about to arrive
            try:
                WebDriverWait(driver, 2).until(lambda d: check_search_limit_error(d))
                return False, "search_limit_exceeded"
            except TimeoutException:
                pass
            return True, "data_loaded"
        return True, "no_data"
    except Exception as e:
        return False, str(e)


# ─── Data extraction ─────────────────────────────────────────────────────────

def _get(driver, fid):
    try:
        el = driver.find_element(By.ID, fid)
        v = el.get_attribute("value") or el.text
        return v.strip() if v else ""
    except Exception:
        return ""


def extract_photo(driver):
    try:
        img = WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.XPATH, "//img[contains(@src,'Photo')]"))
        )
        return img.get_attribute("src")
    except Exception:
        return ""


def download_photo(src, nid):
    if not src:
        return ""
    path = os.path.join(UPLOAD_DIR, f"{nid}.jpg")
    try:
        if "data:image" in src:
            with open(path, "wb") as f:
                f.write(base64.b64decode(src.split(",")[1]))
        else:
            r = requests.get(src, timeout=15)
            with open(path, "wb") as f:
                f.write(r.content)
        return f"http://151.158.158.40:5000/uploads/{nid}.jpg"
    except Exception as e:
        safe_print(f"Photo error: {e}")
        return ""


def extract_all_data(driver, nid, dob, cred_username=""):
    name_bangla = _get(driver, "P600_PERSON_NAME")
    if not name_bangla:
        return {"status": "fail", "message": "No data found"}, "no_data"

    photo_url = download_photo(extract_photo(driver), nid)

    personal_info = {
        "age":           _get(driver, "P600_AGE"),
        "blood_group":   _get(driver, "P600_BLOOD_GR"),
        "date_of_birth": _get(driver, "P600_APP_BORNYEAR"),
        "father_name":   _get(driver, "P600_FATH_NM"),
        "gender":        _get(driver, "P600_GEND"),
        "mother_name":   _get(driver, "P600_MOTH_NM"),
        "name_bangla":   name_bangla,
        "name_english":  _get(driver, "P600_PERSON_NAME_ENG"),
        "national_id":   _get(driver, "P600_NID"),
    }

    with _success_lock:
        _cred_success_counts[cred_username] = _cred_success_counts.get(cred_username, 0) + 1
        sc = _cred_success_counts[cred_username]
        _save_success_counts(_cred_success_counts)

    def addr_line(px):
        parts = [
            ("বাসা/হোল্ডিং",  _get(driver, f"P600_{px}HOMEORHOLDINGNO")),
            ("গ্রাম/রাস্তাঃ",  _get(driver, f"P600_{px}ADDI_VILL_OR_ROAD")),
            ("পোষ্ট অফিসঃ",  _get(driver, f"P600_{px}POSTOFFICE")),
            ("পোষ্ট কোডঃ",   _get(driver, f"P600_{px}POSTALCODE")),
            ("উপজেলাঃ",      _get(driver, f"P600_{px}UPOZILA")),
            ("জেলাঃ",         _get(driver, f"P600_{px}DISTRICT")),
            ("বিভাগঃ",        _get(driver, f"P600_{px}DIVISION")),
        ]
        return ", ".join(f"{k}: {v}" for k, v in parts)

    return {
        "data": {
            "nid": nid, "dob": dob,
            "personal_info": personal_info,
            "present_address": {
                "district":     _get(driver, "P600_PDISTRICT"),
                "division":     _get(driver, "P600_PDIVISION"),
                "full_address": _get(driver, "P600_PRESENT_ADDRESS"),
                "home_holding": _get(driver, "P600_PHOMEORHOLDINGNO"),
                "post_office":  _get(driver, "P600_PPOSTOFFICE"),
                "postal_code":  _get(driver, "P600_PPOSTALCODE"),
                "upazila":      _get(driver, "P600_PUPOZILA"),
                "village_road": _get(driver, "P600_PADDI_VILL_OR_ROAD"),
            },
            "permanent_address": {
                "district":     _get(driver, "P600_DISTRICT"),
                "division":     _get(driver, "P600_DIVISION"),
                "full_address": _get(driver, "P600_PERMANENT_ADDRESS"),
                "home_holding": _get(driver, "P600_HOMEORHOLDINGNO"),
                "post_office":  _get(driver, "P600_POSTOFFICE"),
                "postal_code":  _get(driver, "P600_POSTALCODE"),
                "upazila":      _get(driver, "P600_UPOZILA"),
                "village_road": _get(driver, "P600_ADDI_VILL_OR_ROAD"),
            },
            "perAddress": {"addressLine": addr_line("")},
            "preAddress":  {"addressLine": addr_line("P")},
            "photo": photo_url,
        },
        "status": "success",
        "used_credential": cred_username,
        "success_count_number": sc,
    }, "success"


# ─── Browser pool ────────────────────────────────────────────────────────────

class BrowserPool:
    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._pool      = []
        self._disabled  = self._load_disabled()
        base = [c for c in get_credentials_list()
                if c.get('active', True) and c['username'] not in self._disabled]
        # Repeat each credential BROWSERS_PER_ACCOUNT times so the pool can
        # spawn that many independent APEX sessions per account in parallel
        self._creds    = [c for c in base for _ in range(BROWSERS_PER_ACCOUNT)]
        self._cred_idx = 0

    # Accounts disabled for more than this many hours are re-enabled (daily limit reset)
    _DISABLE_TTL_HOURS = 20

    def _load_disabled(self):
        """Load disabled accounts. Entries older than _DISABLE_TTL_HOURS are dropped
        (daily search limit resets ~midnight, so accounts become usable again)."""
        try:
            with open(DISABLED_FILE, encoding='utf-8') as f:
                content = f.read().strip()
                if not content:
                    return {}
                data = json.loads(content)
            # Support old format (list of usernames) — migrate to dict
            if isinstance(data, list):
                data = {u: "2000-01-01T00:00:00" for u in data}
            now = datetime.utcnow()
            active = {}
            expired = []
            for username, ts in data.items():
                try:
                    disabled_at = datetime.fromisoformat(ts)
                    age_hours = (now - disabled_at).total_seconds() / 3600
                    if age_hours < self._DISABLE_TTL_HOURS:
                        active[username] = ts
                    else:
                        expired.append(username)
                except Exception:
                    active[username] = ts  # keep if timestamp unparseable
            if expired:
                safe_print(f"Re-enabled {len(expired)} account(s) after daily limit reset: {expired}")
            safe_print(f"Loaded {len(active)} disabled accounts (still within TTL)")
            return active
        except FileNotFoundError:
            return {}
        except Exception as e:
            safe_print(f"Warning: could not load {DISABLED_FILE}: {e}")
            return {}

    def _save_disabled(self):
        try:
            with open(DISABLED_FILE, 'w', encoding='utf-8') as f:
                json.dump(self._disabled, f, indent=2)
        except Exception as e:
            safe_print(f"Warning: save disabled failed: {e}")

    def _retire(self, entry, reason):
        """Must be called while holding self._condition.
        Returns list of drivers to quit AFTER the lock is released."""
        username = entry['cred']['username']
        self._disabled[username] = datetime.utcnow().isoformat()
        self._save_disabled()

        # Mark ALL browsers for this account — they share the same daily quota
        siblings = [e for e in self._pool if e['cred']['username'] == username]
        to_quit = []
        for e in siblings:
            e['cred']['active'] = False
            e['limit'] = True
            if e is entry or not e['in_use']:
                # Caller holds primary entry; idle siblings — safe to quit now
                to_quit.append(e['driver'])
                try:
                    self._pool.remove(e)
                except ValueError:
                    pass
            else:
                # In use by another thread — defer quit to release()
                e['_retiring'] = True
        safe_print(f"Retired [{reason}]: {username} × {len(siblings)} browser(s)")
        return to_quit

    def _create_entry(self):
        if self._cred_idx >= len(self._creds):
            raise RuntimeError("All credentials exhausted")
        cred = self._creds[self._cred_idx]
        self._cred_idx += 1
        driver = create_driver()
        entry = {
            'driver':     driver,
            'cred':       cred,
            'in_use':     False,
            'limit':      False,
            'logged_in':  False,
            'verified':   False,   # True after quota check via test NID
            'session_id': None,    # real APEX session ID captured after login
        }
        self._pool.append(entry)
        safe_print(f"Pool: +browser [{len(self._pool)}/{MAX_BROWSERS}] {cred['username']}")
        return entry

    def _ensure_logged_in(self, entry):
        """Login this browser. Returns True on success."""
        if entry['logged_in']:
            return True
        driver   = entry['driver']
        username = entry['cred']['username']
        password = entry['cred']['password']
        ok, status = do_login(driver, username, password)
        if not ok:
            return False
        sid = _get_session_id(driver)
        entry['session_id'] = sid
        entry['logged_in']  = True
        safe_print(f"Login complete: {username}  session={sid}")
        return True

    def prewarm(self, count=PREWARM_COUNT):
        """Find `count` verified working accounts sequentially.
        entry['in_use'] stays True during login+verify so worker threads
        cannot steal the entry before it is ready."""
        safe_print(f"Prewarm: finding {count} working account(s)...")
        found = 0

        while found < count:
            with self._condition:
                if self._cred_idx >= len(self._creds):
                    safe_print(f"Prewarm: credentials exhausted — {found}/{count} ready")
                    self._condition.notify_all()
                    break
                try:
                    entry = self._create_entry()
                    entry['in_use'] = True   # hold until verified
                except Exception as e:
                    safe_print(f"Prewarm create error: {e}")
                    self._condition.notify_all()
                    continue

            # Login — no lock held (browser op)
            try:
                ok = self._ensure_logged_in(entry)
            except Exception as e:
                safe_print(f"Prewarm login error [{entry['cred']['username']}]: {e}")
                ok = False
            if not ok:
                with self._condition:
                    to_quit = self._retire(entry, "prewarm login failed")
                    self._condition.notify_all()
                for d in to_quit:
                    try:
                        d.quit()
                    except Exception:
                        pass
                continue

            # Verify quota — no lock held
            try:
                verdict = _verify_account(entry)
            except Exception as e:
                safe_print(f"Prewarm verify error [{entry['cred']['username']}]: {e}")
                verdict = 'error'

            if verdict == 'limit':
                with self._condition:
                    to_quit = self._retire(entry, "search limit at startup")
                    self._condition.notify_all()
                for d in to_quit:
                    try:
                        d.quit()
                    except Exception:
                        pass
                continue
            elif verdict == 'error':
                with self._condition:
                    to_quit = self._retire(entry, "verify error at startup")
                    self._condition.notify_all()
                for d in to_quit:
                    try:
                        d.quit()
                    except Exception:
                        pass
                continue

            # Account verified — release to pool
            entry['verified'] = True
            found += 1
            safe_print(f"Prewarm [{found}/{count}]: {entry['cred']['username']} ready")
            with self._condition:
                entry['in_use'] = False
                self._condition.notify_all()

        safe_print(f"Prewarm complete: {found}/{count} accounts ready")

    def acquire(self, timeout=120):
        deadline = time.time() + timeout
        with self._condition:
            while True:
                for e in self._pool:
                    if not e['in_use'] and not e['limit']:
                        e['in_use'] = True
                        return e
                if len(self._pool) < MAX_BROWSERS and self._cred_idx < len(self._creds):
                    e = self._create_entry()
                    e['in_use'] = True
                    return e
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("No browser slot available")
                self._condition.wait(timeout=min(remaining, 5))

    def release(self, entry):
        retiring = False
        with self._condition:
            entry['in_use'] = False
            if entry.get('_retiring'):
                retiring = True
                try:
                    self._pool.remove(entry)
                except ValueError:
                    pass
            self._condition.notify_all()
        if retiring:
            try:
                entry['driver'].quit()
            except Exception:
                pass

    def mark_limit(self, entry):
        with self._condition:
            to_quit = self._retire(entry, "search limit exceeded")
            self._condition.notify_all()
        for d in to_quit:
            try:
                d.quit()
            except Exception:
                pass

    def mark_login_failed(self, entry):
        with self._condition:
            to_quit = self._retire(entry, "login failed")
            self._condition.notify_all()
        for d in to_quit:
            try:
                d.quit()
            except Exception:
                pass

    @property
    def stats(self):
        with self._condition:
            with _success_lock:
                counts = dict(_cred_success_counts)
            return {
                'total':              len(self._pool),
                'in_use':             sum(1 for e in self._pool if e['in_use']),
                'logged_in':          sum(1 for e in self._pool if e['logged_in']),
                'limited':            sum(1 for e in self._pool if e['limit']),
                'disabled_total':     len(self._disabled),
                'max':                MAX_BROWSERS,
                'creds_remaining':    len(self._creds) - self._cred_idx,
                'success_per_cred':   counts,
            }


_pool = BrowserPool()


# ─── Per-request processing (same window, no tabs — avoids session confusion) ──

def _do_login_for_entry(entry):
    """Login and capture the real APEX session ID. Returns True on success."""
    driver   = entry['driver']
    username = entry['cred']['username']
    password = entry['cred']['password']
    ok, status = do_login(driver, username, password)
    if not ok:
        return False
    sid = _get_session_id(driver)
    entry['session_id'] = sid
    entry['logged_in']  = True
    safe_print(f"Login OK: {username}  session={sid}")
    return True


def _check_nid_filled(driver, nid):
    """Verify APEX applied the correct NID to the search input field."""
    try:
        el = driver.find_element(By.ID, "P600_NATIONALID")
        val = (el.get_attribute("value") or el.text or "").strip()
        safe_print(f"NID field: got='{val}' expected='{nid}'")
        return val == nid
    except Exception:
        safe_print("NID field not found on page")
        return False


def _verify_account(entry):
    """Run a test NID search to confirm account has remaining quota.
    Returns 'ok', 'limit', or 'error'."""
    driver   = entry['driver']
    username = entry['cred']['username']
    sid      = entry.get('session_id') or '115196342139283'

    safe_print(f"Verifying account {username} — test NID {TEST_NID}...")
    driver.get(_nid_url(TEST_NID, TEST_DOB, session=sid))
    _wait_for_page(driver, timeout=15)

    if on_login_page(driver):
        safe_print(f"Session lost during verify: {username}")
        return 'error'
    # Limit banner can appear on page load before any click
    if check_search_limit_error(driver):
        safe_print(f"Account {username} hit limit (on page load) — will try next")
        return 'limit'
    if not on_data_page(driver):
        safe_print(f"Unexpected page during verify: {username}: {driver.current_url[:80]}")
        return 'error'
    if not _check_nid_filled(driver, TEST_NID):
        safe_print(f"NID field empty during verify: {username}")
        return 'error'

    ok, status = click_search_and_wait(driver)
    if status == "search_limit_exceeded":
        safe_print(f"Account {username} hit limit (after search) — will try next")
        return 'limit'
    if not ok:
        safe_print(f"Account {username} verify search failed: {status}")
        return 'error'

    safe_print(f"Account {username} verified OK (search status={status})")
    return 'ok'


def _search_in_tab(entry, nid, dob):
    """Run one NID search inside a dedicated tab. Tab must already be active."""
    driver   = entry['driver']
    username = entry['cred']['username']
    base_handle = entry.get('_base_handle')

    sid = entry.get('session_id') or '115196342139283'
    safe_print(f"[tab] Navigating NID={nid}")
    driver.get(_nid_url(nid, dob, session=sid))
    _wait_for_page(driver, timeout=15)

    # Session expired in tab → re-login in base window, then retry in this tab
    if on_login_page(driver):
        safe_print(f"Session expired in tab [{username}] — re-login in base window")
        driver.switch_to.window(base_handle)
        entry['logged_in'] = False
        if not _do_login_for_entry(entry):
            driver.switch_to.window(entry['_tab_handle'])
            return None, "login_failed:relogin"
        driver.switch_to.window(entry['_tab_handle'])
        sid = entry.get('session_id') or '115196342139283'
        driver.get(_nid_url(nid, dob, session=sid))
        _wait_for_page(driver, timeout=15)

    if check_search_limit_error(driver):
        safe_print(f"Limit on page load [{username}]")
        return None, "search_limit_exceeded"

    apex_err = get_apex_error(driver)
    if apex_err:
        safe_print(f"APEX error on page load [{username}]: {apex_err}")
        return None, apex_err

    if not on_data_page(driver):
        safe_print(f"Unexpected page [{username}]: {driver.current_url[:100]}")
        return None, "unexpected_page"

    if not _check_nid_filled(driver, nid):
        safe_print(f"NID field mismatch after navigation [{username}]")
        return None, "nid_not_filled"

    ok, status = click_search_and_wait(driver)
    if not ok:
        return None, status

    result_data, result_status = extract_all_data(driver, nid, dob, username)
    if result_status == "success":
        scraped_nid = result_data.get("data", {}).get("personal_info", {}).get("national_id", "")
        if scraped_nid and scraped_nid != nid:
            if check_search_limit_error(driver):
                safe_print(f"NID mismatch caused by limit [{username}]")
                return None, "search_limit_exceeded"
            safe_print(f"NID mismatch [{username}]: req={nid} scraped={scraped_nid}")
            return None, "nid_mismatch"
    return result_data, result_status


def _process_request(entry, nid, dob):
    """Open a new tab for each request, close it when done. Browser stays alive."""
    driver   = entry['driver']
    username = entry['cred']['username']

    # Ensure logged in (uses base window)
    if not entry['logged_in']:
        ok = _pool._ensure_logged_in(entry)
        if not ok:
            return None, "login_failed:ensure_login"

    # Verify quota with test NID before first real search (uses base window)
    if not entry.get('verified'):
        safe_print(f"Verifying {username} before real search...")
        verdict = _verify_account(entry)
        if verdict == 'limit':
            return None, "search_limit_exceeded"
        elif verdict == 'error':
            return None, "login_failed:verify_error"
        entry['verified'] = True

    # Remember base window, open a fresh tab for this request
    base_handle = driver.current_window_handle
    entry['_base_handle'] = base_handle

    handles_before = set(driver.window_handles)
    driver.execute_script("window.open('');")
    tab_handle = (set(driver.window_handles) - handles_before).pop()
    entry['_tab_handle'] = tab_handle
    driver.switch_to.window(tab_handle)
    safe_print(f"[tab] Opened for NID={nid} [{username}]")

    try:
        return _search_in_tab(entry, nid, dob)
    finally:
        # Always close tab and return to base window regardless of outcome
        try:
            driver.close()
        except Exception:
            pass
        try:
            driver.switch_to.window(base_handle)
        except Exception:
            pass
        safe_print(f"[tab] Closed [{username}]")


# ─── Public API ──────────────────────────────────────────────────────────────

def login_cdms(nid, dob):
    max_tries = len(get_credentials_list()) + 5
    entry = None  # held across iterations so same account can be retried

    for attempt in range(max_tries):
        # Acquire only when we don't already hold a locked entry
        if entry is None:
            try:
                entry = _pool.acquire(timeout=120)
            except Exception as e:
                safe_print(f"Acquire failed: {e}")
                break

        username = entry['cred']['username']
        safe_print(f"[attempt {attempt + 1}] account: {username}")

        try:
            data, status = _process_request(entry, nid, dob)
        except Exception as e:
            safe_print(f"Request exception [{username}]: {e}")
            _pool.release(entry)
            entry = None
            continue

        # ── retire account ────────────────────────────────────────────────
        if status == "search_limit_exceeded":
            safe_print(f"Limit hit — retiring {username}")
            _pool.mark_limit(entry)
            entry = None
            continue

        if data is None and "login_failed" in status:
            safe_print(f"Login failed — retiring {username}")
            _pool.mark_login_failed(entry)
            entry = None
            continue

        # ── immediate user error — no retry ──────────────────────────────
        if status == "invalid_nid_dob":
            _pool.release(entry)
            entry = None
            return {"status": "fail", "message": "Invalid NID or Date of Birth"}, "invalid_nid_dob"

        # ── re-login SAME account, keep entry locked ──────────────────────
        if status == "auth_required":
            safe_print(f"Auth required [{username}] — re-login same account")
            entry['logged_in'] = False
            entry['session_id'] = None
            entry['verified']   = False
            ok = _pool._ensure_logged_in(entry)
            if not ok:
                safe_print(f"Re-login failed [{username}] — retiring")
                _pool.mark_login_failed(entry)
                entry = None
            # entry stays locked → next iteration retries same account
            continue

        # ── release + retry (different or same account) ───────────────────
        if status == "missing_fields":
            safe_print(f"Missing fields [{username}] — reset session and retry")
            entry['logged_in'] = False
            entry['session_id'] = None
            _pool.release(entry)
            entry = None
            continue

        if status in ("nid_mismatch", "unexpected_page"):
            safe_print(f"{status} [{username}] — retry")
            _pool.release(entry)
            entry = None
            continue

        if status == "nid_not_filled":
            safe_print(f"NID not filled [{username}] — reset session and retry")
            entry['logged_in'] = False
            entry['session_id'] = None
            _pool.release(entry)
            entry = None
            continue

        # ── success / no_data ─────────────────────────────────────────────
        _pool.release(entry)
        entry = None

        if status == "no_data":
            return data, status

        if data is None:
            safe_print(f"Null result [{username}] status={status} — retry")
            continue

        return data, status

    if entry is not None:
        _pool.release(entry)
    return {"status": "fail", "message": "All credentials exhausted"}, "all_exhausted"


# ─── Request queue ────────────────────────────────────────────────────────────

class _Request:
    __slots__ = ('nid', 'dob', 'event', 'result', 'queued_at', 'started_at', 'finished_at')
    def __init__(self, nid, dob):
        self.nid         = nid
        self.dob         = dob
        self.event       = threading.Event()
        self.result      = None
        self.queued_at   = time.monotonic()
        self.started_at  = None
        self.finished_at = None

_request_queue = _q.Queue(maxsize=MAX_QUEUE_SIZE)


def _worker():
    """One worker thread — drains the request queue using the browser pool."""
    while True:
        try:
            req = _request_queue.get(timeout=5)
        except _q.Empty:
            continue
        req.started_at = time.monotonic()
        try:
            data, status = login_cdms(req.nid, req.dob)
            req.result = (data, status)
        except Exception as e:
            req.result = ({"status": "fail", "message": str(e)}, "error")
        finally:
            req.finished_at = time.monotonic()
            req.event.set()
            _request_queue.task_done()


def start_workers(n=MAX_BROWSERS):
    """Start n worker threads. Call once at server startup."""
    for _ in range(n):
        threading.Thread(target=_worker, daemon=True).start()
    safe_print(f"Started {n} request worker threads")


def login_cdms_with_limit(nid, dob, output_file=None):
    # Lifetime cache — hit before touching the browser pool
    cached = cache_get(nid, dob)
    if cached is not None:
        safe_print(f"✅ Cache hit: NID {nid}")
        return {"success": True, "data": cached, "status": "success", "cached": True,
                "duration": {"total_seconds": 0, "queue_wait_seconds": 0, "process_seconds": 0}}

    safe_print(f"Queuing NID: {nid}")
    req = _Request(nid, dob)
    try:
        _request_queue.put_nowait(req)
    except _q.Full:
        safe_print(f"Queue full — rejected NID: {nid}")
        return {"success": False, "error": "Server busy, try again later", "status": "queue_full"}

    req.event.wait(timeout=300)
    if req.result is None:
        return {"success": False, "error": "Request timed out", "status": "timeout"}

    now      = time.monotonic()
    finished = req.finished_at or now
    started  = req.started_at  or req.queued_at
    duration = {
        "total_seconds":      round(finished - req.queued_at, 2),
        "queue_wait_seconds": round(started  - req.queued_at, 2),
        "process_seconds":    round(finished - started,       2),
    }

    data, status = req.result
    if status == "success":
        cache_set(nid, dob, data)
        safe_print(f"✅ NID {nid} done in {duration['total_seconds']}s — saved to cache")
        return {"success": True, "data": data, "status": status, "duration": duration}
    safe_print(f"❌ NID {nid} failed: {status} in {duration['total_seconds']}s")
    return {"success": False, "data": data, "error": status, "status": status, "duration": duration}


# ─── Standalone batch runner ─────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1].endswith('.json'):
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            nid_list = json.load(f)
        if PREWARM_COUNT:
            _pool.prewarm(min(PREWARM_COUNT, len(nid_list)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_BROWSERS) as ex:
            futures = {ex.submit(login_cdms_with_limit, i['nid'], i['dob']): i
                       for i in nid_list}
            for f in concurrent.futures.as_completed(futures):
                try:
                    f.result()
                except Exception as e:
                    safe_print(f"Thread error: {e}")
        safe_print("All done!")
    elif len(sys.argv) >= 3:
        data, status = login_cdms(sys.argv[1], sys.argv[2])
        print(f"Status: {status}")
        print(f"Result: {data}")
    else:
        safe_print("Usage: python cdms_script.py <list.json>  OR  <nid> <dob>")
