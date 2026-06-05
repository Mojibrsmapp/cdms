from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import time
import json
import threading

# cdms_script থেকে প্রয়োজনীয় ফাংশন ও গ্লোবাল পুল অবজেক্ট ইম্পোর্ট করা
from cdms_script import login_cdms_with_limit, start_workers, _pool, cache_get, cache_set

app = Flask(__name__)
CORS(app)

API_KEY   = os.environ.get("CDMS_API_KEY", "BBbrrsfn8fls8jflsbfiks")
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# ─── Configuration ────────────────────────────────────────────────────────────
_cache_enabled = True  # ডিফল্ট লোকাল ক্যাশ অপশন চালু রাখা হলো যাতে একই NID বারবার কুয়েরি না হয়


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route('/api/search', methods=['GET'])
def search_nid():
    global _cache_enabled
    try:
        nid       = request.args.get('nid',   '').strip()
        dob       = request.args.get('dob',   '').strip()
        key       = request.args.get('key',   '')
        use_cache = request.args.get('cache', '').lower() not in ('0', 'false', 'no')

        if key != API_KEY:
            return jsonify({"success": False, "error": "Invalid API key"}), 401
        if not nid or not dob:
            return jsonify({"success": False, "error": "NID and DOB required"}), 400
        if not nid.isdigit() or len(nid) not in (10, 13, 17):
            return jsonify({"success": False, "error": "Invalid NID format"}), 400

        # গ্লোবাল ক্যাশ হিট চেকিং
        if _cache_enabled and use_cache:
            cached = cache_get(nid, dob)
            if cached is not None:
                return jsonify({
                    "success": True, 
                    "data": cached, 
                    "status": "success",
                    "cached": True, 
                    "duration": {"total_seconds": 0}
                })

        # সেলেনিয়ামের থ্রেড-বেসড কিউ সিস্টেমে রিকোয়েস্ট পাঠানো
        res = login_cdms_with_limit(nid, dob)
        
        if res.get("success"):
            return jsonify(res)
        else:
            # যদি পুল এক্সহস্টেড বা লিমিট এরর আসে
            if res.get("status") in ("search_limit_exceeded", "all_exhausted"):
                return jsonify(res), 503
            return jsonify(res), 500

    except Exception as e:
        print(f"Error in search: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/test', methods=['GET'])
def test():
    return jsonify({"success": True, "message": "API is working"})


# ─── Pool Stats & Reset ───────────────────────────────────────────────────────

@app.route('/api/pool', methods=['GET'])
def pool_stats():
    # cdms_script এর BrowserPool লাইভ স্ট্যাটাস রিটার্ন করবে
    try:
        return jsonify(_pool.stats)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/pool/reset', methods=['POST'])
def pool_reset():
    """সেলেনিয়াম ড্রাইভার পুলের লিমিট এবং ট্র্যাকিং রিসেট করার রুট"""
    key = request.args.get('key', '')
    if key != API_KEY:
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    
    try:
        # ওল্ড লক বা লিমিটেড অ্যাকাউন্টগুলো রিলিজ করতে ট্র্যাকিং ফাইল রিসেট
        with open("disabled_creds.json", "w", encoding="utf-8") as f:
            json.dump({}, f)
        
        # পুল অবজেক্টের ইন্টারনাল ডিসেবলড অ্যাকাউন্ট ট্র্যাকার রিলিজ করা
        if hasattr(_pool, '_disabled'):
            _pool._disabled = {}
        
        return jsonify({
            "success": True, 
            "message": "Selenium browser pool limits and disabled_creds.json cleared successfully."
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


# ─── Cache control ────────────────────────────────────────────────────────────

@app.route('/api/cache', methods=['GET'])
def cache_status():
    files = [f for f in os.listdir(CACHE_DIR) if f.endswith('.json')]
    return jsonify({
        "enabled":    _cache_enabled,
        "file_count": len(files),
    })


@app.route('/api/cache/toggle', methods=['POST'])
def cache_toggle():
    global _cache_enabled
    key = request.args.get('key', '')
    if key != API_KEY:
        return jsonify({"success": False, "error": "Invalid API key"}), 401

    enable = request.args.get('enable', '').lower()
    if enable in ('1', 'true', 'yes', 'on'):
        _cache_enabled = True
    elif enable in ('0', 'false', 'no', 'off'):
        _cache_enabled = False
    else:
        _cache_enabled = not _cache_enabled

    return jsonify({"success": True, "cache_enabled": _cache_enabled})


@app.route('/api/cache/clear', methods=['POST'])
def cache_clear():
    key = request.args.get('key', '')
    if key != API_KEY:
        return jsonify({"success": False, "error": "Invalid API key"}), 401

    nid = request.args.get('nid', '').strip()
    dob = request.args.get('dob', '').strip()

    if nid and dob:
        path = os.path.join(CACHE_DIR, f"{nid}_{dob}.json")
        if os.path.exists(path):
            os.remove(path)
            return jsonify({"success": True, "deleted": 1})
        return jsonify({"success": True, "deleted": 0})

    count = 0
    for f in os.listdir(CACHE_DIR):
        if f.endswith('.json'):
            try:
                os.remove(os.path.join(CACHE_DIR, f))
                count += 1
            except Exception:
                pass
    return jsonify({"success": True, "deleted": count})


if __name__ == '__main__':
    print("Starting Server — Selenium Browser Automation Pool Mode")
    
    # cdms_script এর বিল্ট-ইন মেথড ব্যবহার করে ব্যাকগ্রাউন্ড ওয়ার্কার থ্রেড চালু করা
    # এটি প্রজেক্টের রানিং ক্যাপাসিটি অনুযায়ী ২টি সমান্তরাল ব্রাউজার সেশন রেডি রাখবে
    start_workers(n=2) 
    
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
