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
MAX_QUEUE_SIZE       = 50  
HEADLESS             = True
BROWSERS_PER_ACCOUNT = 1    
PREWARM_COUNT        = 1    # 💡 এটি '0' থেকে পরিবর্তন করে '1' করুন (যাতে সার্ভার চালুর সাথে সাথেই ব্রাউজার ওপেন হয়)
MAX_BROWSERS         = 2    # 💡 এটি ২ রাখুন

CACHE_DIR           = "cache"
UPLOAD_DIR          = "uploads"
DISABLED_FILE       = "disabled_creds.json"
SUCCESS_COUNTS_FILE = "success_counts.json"


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
       'Chrome/148.0.7778.215 Safari/537.36')

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


# ─── Result cache ────────────────────────────────────────────────────────────

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


# ─── Driver factory (Tor Connected) ──────────────────────────────────────────

def create_driver():
    from selenium.webdriver.chrome.options import Options
    opts = Options()
    
    opts.add_argument('--headless=new') 
    opts.add_argument('--no-sandbox')
    opts.add_argument('--disable-dev-shm-usage')
    opts.add_argument('--disable-gpu')
    opts.add_argument('--window-size=1280,720')
    opts.add_argument('--disable-blink-features=AutomationControlled')
    
    # 💡 Docker + Tor নেটওয়ার্ক লোকালহোস্ট গেটওয়ে পোর্ট
    opts.add_argument('--proxy-server=socks5://127.0.0.1:9050')
    opts.binary_location = "/usr/bin/chromium"
    
    opts.add_argument(f'--user-agent={_UA}')
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option('useAutomationExtension', False)
    
    from selenium.webdriver.chrome.service import Service
    service = Service(executable_path="/usr/bin/chromium-driver")
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
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
    try:
        el = driver.find_element(By.ID, "P600_PERSON_NAME")
        v = (el.get_attribute("value") or el.text or "").strip()
        return bool(v)
    except Exception:
        return False


def check_password_change_page(driver):
    try:
        t = _src(driver).lower()
        return any(k in t for k in ["password change", "change password", "enter otp", "পাসওয়ার্ড পরিবর্তন"])
    except Exception:
        return False


_LIMIT_KEYWORDS = ("এনআইডি সার্চ", "সর্বোচ্চ সার্চের সীমা", "বার এনআইডি সার্চ করেছেন")

def _has_limit_text(txt):
    return any(k in txt for k in _LIMIT_KEYWORDS)


def check_search_limit_error(driver):
    try:
        el = driver.find_element(By.ID, "t_Alert_Notification")
        if _has_limit_text(el.get_attribute("textContent") or ""): return True
    except Exception: pass
    try:
        for item in driver.find_elements(By.CSS_SELECTOR, ".htmldbStdErr"):
            if _has_limit_text(item.get_attribute("textContent") or ""): return True
    except Exception: pass
    try:
        s = _src(driver)
        return ("আপনি আজকে" in s and "সর্বোচ্চ সার্চের সীমা" in s)
    except Exception: return False


_APEX_ERRORS = {
    "invalid nid or date of birth": "invalid_nid_dob",
    "full authentication is required to access this resource": "auth_required",
    "search permission without one of the mandatory fields": "missing_fields",
}

def get_apex_error(driver):
    def _match(txt):
        t = (txt or "").lower()
        for phrase, key in _APEX_ERRORS.items():
            if phrase in t: return key
        return None
    try:
        r = _match(driver.page_source)
        if r: return r
    except Exception: pass
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
    element.clear()
    for char in text:
        element.send_keys(char)
        time.sleep(random.uniform(0.05, 0.10))


def handle_password_change(driver):
    for attempt in [
        lambda: driver.back(),
        lambda: driver.find_element(By.CSS_SELECTOR, "input[value='Cancel']").click(),
    ]:
        try:
            attempt()
            time.sleep(0.5)
            return True
        except Exception:
            continue
    return False


def perform_login(driver, username, password):
    try:
        user_field = WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.ID, "P101_USERNAME"))
        )
        _type_field(user_field, username)
        pwd_field = driver.find_element(By.ID, "P101_PASSWORD")
        _type_field(pwd_field, password)

        driver.execute_script("arguments[0].click();", driver.find_element(By.ID, "P101_LOGIN"))
        
        try:
            WebDriverWait(driver, 25).until(lambda d: (
                on_data_page(d) or check_password_change_page(d) or check_stuck_page(d) or not on_login_page(d)
            ))
        except TimeoutException:
            pass

        if check_automation_blocked(driver): return False, "automation_blocked"
        if check_password_change_page(driver):
            if not handle_password_change(driver): return False, "password_change_required"
        if check_stuck_page(driver): return False, "stuck_page"
        if on_data_page(driver): return True, "success"
        if on_login_page(driver): return False, "login_failed"
        return True, "unknown_page"
    except Exception as e:
        return False, str(e)


# ─── Session helpers ──────────────────────────────────────────────────────────

def _get_session_id(driver):
    try:
        m = re.search(r'f\?p=\d+:\d+:(\d+)', driver.current_url)
        if m and m.group(1) not in ('0', ''): return m.group(1)
    except Exception: pass
    return None


def _nid_url(nid, dob, session=''):
    return f"{_CDMS_BASE}:600:{session}:::600:P600_NATIONALID,P600_DOB:{nid},{dob}"


def _wait_for_page(driver, timeout=15):
    try:
        WebDriverWait(driver, timeout).until(lambda d: on_login_page(d) or on_data_page(d))
    except TimeoutException: pass


def do_login(driver, username, password):
    try:
        driver.get(f"{_CDMS_DOMAIN}/cdms/cdms_pool/")
        driver.delete_all_cookies()
    except Exception: pass
    
    # 💡 হার্ডকোডেড পুরনো সেশন আইডি বাদ দিয়ে ফ্রেশ রিকোয়েস্ট জেনারেট করা
    driver.get(f"{_CDMS_BASE}:600:")
    _wait_for_page(driver, timeout=15)

    if not on_login_page(driver):
        return True, "already_logged_in"

    ok, status = perform_login(driver, username, password)
    return ok, status


# ─── Search ──────────────────────────────────────────────────────────────────

def click_search_and_wait(driver):
    try:
        btn = WebDriverWait(driver, 20).until(EC.element_to_be_clickable((By.ID, "B635854564534766524")))
        driver.execute_script("""
            ['P600_PERSON_NAME','P600_NID','P600_FATH_NM','P600_PERSON_NAME_ENG'].forEach(function(id){
                var el = document.getElementById(id); if (el) el.value = '';
            });
        """)
        driver.execute_script("arguments[0].click();", btn)
        try:
            WebDriverWait(driver, 45).until(lambda d: check_search_limit_error(d) or check_data_loaded(d) or get_apex_error(d))
        except TimeoutException: pass
        
        if check_search_limit_error(driver): return False, "search_limit_exceeded"
        apex_err = get_apex_error(driver)
        if apex_err: return False, apex_err
        if check_data_loaded(driver): return True, "data_loaded"
        return True, "no_data"
    except Exception as e:
        return False, str(e)


# ─── Data extraction ─────────────────────────────────────────────────────────

def _get(driver, fid):
    try:
        el = driver.find_element(By.ID, fid)
        v = el.get_attribute("value") or el.text
        return v.strip() if v else ""
    except Exception: return ""


def extract_photo(driver):
    try:
        img = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.XPATH, "//img[contains(@src,'Photo')]")))
        return img.get_attribute("src")
    except Exception: return ""


def download_photo(src, nid):
    if not src: return ""
    path = os.path.join(UPLOAD_DIR, f"{nid}.jpg")
    try:
        if "data:image" in src:
            with open(path, "wb") as f: f.write(base64.b64decode(src.split(",")[1]))
        else:
            r = requests.get(src, timeout=15)
            with open(path, "wb") as f: f.write(r.content)
        return f"/uploads/{nid}.jpg"
    except Exception: return ""


def extract_all_data(driver, nid, dob, cred_username=""):
    name_bangla = _get(driver, "P600_PERSON_NAME")
    if not name_bangla: return {"status": "fail", "message": "No data found"}, "no_data"

    photo_url = download_photo(extract_photo(driver), nid)
    personal_info = {
        "age": _get(driver, "P600_AGE"),
        "blood_group": _get(driver, "P600_BLOOD_GR"),
        "date_of_birth": _get(driver, "P600_APP_BORNYEAR"),
        "father_name": _get(driver, "P600_FATH_NM"),
        "gender": _get(driver, "P600_GEND"),
        "mother_name": _get(driver, "P600_MOTH_NM"),
        "name_bangla": name_bangla,
        "name_english": _get(driver, "P600_PERSON_NAME_ENG"),
        "national_id": _get(driver, "P600_NID"),
    }

    with _success_lock:
        _cred_success_counts[cred_username] = _cred_success_counts.get(cred_username, 0) + 1
        sc = _cred_success_counts[cred_username]
        _save_success_counts(_cred_success_counts)

    return {
        "data": {
            "nid": nid, "dob": dob, "personal_info": personal_info, "photo": photo_url,
            "present_address": {"full_address": _get(driver, "P600_PRESENT_ADDRESS")},
            "permanent_address": {"full_address": _get(driver, "P600_PERMANENT_ADDRESS")}
        },
        "status": "success", "used_credential": cred_username, "success_count_number": sc,
    }, "success"


# ─── Browser pool ────────────────────────────────────────────────────────────

class BrowserPool:
    def __init__(self):
        self._condition = threading.Condition(threading.Lock())
        self._pool      = []
        self._disabled  = self._load_disabled()
        base = [c for c in get_credentials_list() if c.get('active', True) and c['username'] not in self._disabled]
        self._creds    = [c for c in base for _ in range(BROWSERS_PER_ACCOUNT)]
        self._cred_idx = 0

    _DISABLE_TTL_HOURS = 20

    def _load_disabled(self):
        try:
            with open(DISABLED_FILE, encoding='utf-8') as f:
                content = f.read().strip()
                if not content: return {}
                data = json.loads(content)
            if isinstance(data, list): data = {u: "2000-01-01T00:00:00" for u in data}
            return data
        except Exception: return {}

    def _save_disabled(self):
        try:
            with open(DISABLED_FILE, 'w', encoding='utf-8') as f: json.dump(self._disabled, f, indent=2)
        except Exception: pass

    def _retire(self, entry, reason):
        username = entry['cred']['username']
        self._disabled[username] = datetime.utcnow().isoformat()
        self._save_disabled()
        siblings = [e for e in self._pool if e['cred']['username'] == username]
        to_quit = []
        for e in siblings:
            e['cred']['active'] = False
            e['limit'] = True
            to_quit.append(e['driver'])
            try: self._pool.remove(e)
            except ValueError: pass
        safe_print(f"Retired [{reason}]: {username}")
        return to_quit

    def _create_entry(self):
        if self._cred_idx >= len(self._creds): raise RuntimeError("All credentials exhausted")
        cred = self._creds[self._cred_idx]
        self._cred_idx += 1
        driver = create_driver()
        entry = {
            'driver': driver, 'cred': cred, 'in_use': False, 'limit': False,
            'logged_in': False, 'verified': False, 'session_id': None,
        }
        self._pool.append(entry)
        return entry

    def _ensure_logged_in(self, entry):
        if entry['logged_in']: return True
        driver = entry['driver']
        ok, status = do_login(driver, entry['cred']['username'], entry['cred']['password'])
        if not ok: return False
        entry['session_id'] = _get_session_id(driver)
        entry['logged_in']  = True
        return True

    def prewarm(self, count=PREWARM_COUNT):
        """মেমোরি লক বাইপাস করে সরাসরি ব্রাউজার পুল ইনিশিয়ালাইজ করার লজিক"""
        if count <= 0: return
        safe_print(f"🌟 [Prewarm] Initializing {count} background browser connection via Tor...")
        
        def _bg_init():
            try:
                # সরাসরি একটি ফ্রেশ এন্ট্রি তৈরি করে পুলে পুশ করা
                entry = self._create_entry()
                ok = self._ensure_logged_in(entry)
                if ok:
                    entry['verified'] = True
                    safe_print(f"✅ [Prewarm] Background Browser Session Ready: {entry['cred']['username']}")
                else:
                    with self._condition:
                        self._retire(entry, "prewarm startup login failed")
            except Exception as e:
                safe_print(f"❌ [Prewarm] Startup Error: {e}")
                
        threading.Thread(target=_bg_init, daemon=True).start()

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
                if remaining <= 0: raise TimeoutError("No browser slot available")
                self._condition.wait(timeout=min(remaining, 5))

    def release(self, entry):
        with self._condition:
            entry['in_use'] = False
            self._condition.notify_all()

    def mark_limit(self, entry):
        with self._condition: to_quit = self._retire(entry, "search limit exceeded")
        for d in to_quit:
            try: d.quit()
            except Exception: pass

    def mark_login_failed(self, entry):
        with self._condition: to_quit = self._retire(entry, "login failed")
        for d in to_quit:
            try: d.quit()
            except Exception: pass

    @property
    def stats(self):
        return {'total': len(self._pool), 'in_use': sum(1 for e in self._pool if e['in_use']), 'disabled_total': len(self._disabled)}


_pool = BrowserPool()


# ─── Dedicated Request Processing (Single Window - Safe for Tor) ─────────────

def _search_in_session(entry, nid, dob):
    driver   = entry['driver']
    username = entry['cred']['username']
    sid      = entry.get('session_id') or ''

    driver.get(_nid_url(nid, dob, session=sid))
    _wait_for_page(driver, timeout=15)

    if on_login_page(driver):
        entry['logged_in'] = False
        if not _pool._ensure_logged_in(entry): return None, "login_failed"
        sid = entry.get('session_id') or ''
        driver.get(_nid_url(nid, dob, session=sid))
        _wait_for_page(driver, timeout=15)

    if check_search_limit_error(driver): return None, "search_limit_exceeded"
    if not on_data_page(driver): return None, "unexpected_page"

    ok, status = click_search_and_wait(driver)
    if not ok: return None, status

    return extract_all_data(driver, nid, dob, username)


def _process_request(entry, nid, dob):
    """Tor নেটওয়ার্কের স্থায়িত্ব রক্ষার্থে নো-ট্যাব সিঙ্গেল উইন্ডো মেথড"""
    if not entry['logged_in']:
        if not _pool._ensure_logged_in(entry): return None, "login_failed"

    # 💡 এই ৩টি লাইন কেটে দিন অথবা আগে '#' দিয়ে কমেন্ট করে বন্ধ করুন:
    # if not entry.get('verified'):
    #     verdict = _verify_account(entry)
    #     if verdict == 'limit': return None, "search_limit_exceeded"
    #     elif verdict == 'error': return None, "login_failed"
    #     entry['verified'] = True

    try:
        return _search_in_session(entry, nid, dob)
    except Exception as e:
        return None, str(e)


# ─── Public API & Workers ────────────────────────────────────────────────────

def login_cdms(nid, dob):
    max_tries = len(get_credentials_list()) + 2
    entry = None
    for attempt in range(max_tries):
        if entry is None:
            try: entry = _pool.acquire(timeout=60)
            except Exception: break

        username = entry['cred']['username']
        try:
            data, status = _process_request(entry, nid, dob)
        except Exception:
            _pool.release(entry); entry = None; continue

        if status == "search_limit_exceeded":
            _pool.mark_limit(entry); entry = None; continue
        if data is None and "login_failed" in status:
            _pool.mark_login_failed(entry); entry = None; continue
        if status == "invalid_nid_dob":
            _pool.release(entry); return {"status": "fail", "message": "Invalid NID/DOB"}, "invalid_nid_dob"

        _pool.release(entry)
        return data, status

    return {"status": "fail", "message": "All credentials exhausted"}, "all_exhausted"


class _Request:
    __slots__ = ('nid', 'dob', 'event', 'result', 'queued_at', 'started_at', 'finished_at')
    def __init__(self, nid, dob):
        self.nid = nid; self.dob = dob; self.event = threading.Event()
        self.result = None; self.queued_at = time.monotonic(); self.started_at = None; self.finished_at = None

_request_queue = _q.Queue(maxsize=MAX_QUEUE_SIZE)

def _worker():
    while True:
        try: req = _request_queue.get(timeout=5)
        except _q.Empty: continue
        req.started_at = time.monotonic()
        try: req.result = login_cdms(req.nid, req.dob)
        except Exception as e: req.result = ({"status": "fail", "message": str(e)}, "error")
        finally:
            req.finished_at = time.monotonic()
            req.event.set(); _request_queue.task_done()

def start_workers(n=MAX_BROWSERS):
    for _ in range(n): threading.Thread(target=_worker, daemon=True).start()

def login_cdms_with_limit(nid, dob):
    cached = cache_get(nid, dob)
    if cached is not None:
        return {"success": True, "data": cached, "status": "success", "cached": True, "duration": {"total_seconds": 0}}

    req = _Request(nid, dob)
    try: _request_queue.put_nowait(req)
    except _q.Full: return {"success": False, "error": "Server busy", "status": "queue_full"}

    req.event.wait(timeout=300)
    if req.result is None: return {"success": False, "error": "Request timed out", "status": "timeout"}

    data, status = req.result
    if status == "success":
        cache_set(nid, dob, data)
        return {"success": True, "data": data, "status": status}
    return {"success": False, "data": data, "error": status, "status": status}

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        data, status = login_cdms(sys.argv[1], sys.argv[2])
        print(f"Status: {status}\nResult: {data}")
