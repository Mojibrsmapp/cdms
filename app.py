from flask import Flask, request, jsonify
from flask_cors import CORS
import os
import time
import json
import threading

# 💡 ওল্ড cdms_fast পুল সম্পূর্ণ বাদ দিয়ে নতুন cdms_script এর ওয়ার্কার ও পুল অবজেক্ট ইম্পোর্ট করা
from cdms_script import login_cdms_with_limit, start_workers, _pool, cache_get, cache_set

app = Flask(__name__)
CORS(app)

API_KEY   = os.environ.get("CDMS_API_KEY", "BBbrrsfn8fls8jflsbfiks")
CACHE_DIR = "cache"
os.makedirs(CACHE_DIR, exist_ok=True)

# ─── Configuration ────────────────────────────────────────────────────────────
_cache_enabled = True  # ক্লাউডে ডেপ্লয়মেন্টের জন্য ক্যাশ ডিফল্ট True রাখা হলো


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

        # গ্লোবাল ক্যাশ চেক
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

        # 💡 নতুন থ্রেড-বেসড সেলেনিয়াম কিউ স্ক্রিপ্ট এক্সিকিউট করা
        res = login_cdms_with_limit(nid, dob)
        
        if res.get("success"):
            return jsonify(res)
        else:
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
    """লাইভ সেলেনিয়াম ব্রাউজার পুলের কারেন্ট স্ট্যাটাস রিটার্ন করবে"""
    try:
        return jsonify(_pool.stats)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route('/api/pool/reset', methods=['POST'])
def pool_reset():
    """লাইভ সেলেনিয়াম ড্রাইভার পুলের লিমিট এবং ট্র্যাকিং মেমোরি ১০০% রিসেট করার রুট"""
    key = request.args.get('key', '')
    if key != API_KEY:
        return jsonify({"success": False, "error": "Invalid API key"}), 401
    
    try:
        # disabled_creds.json ফাইল রিসেট করা
        with open("disabled_creds.json", "w", encoding="utf-8") as f:
            json.dump({}, f)
        
        # মেমোরিতে আটকে থাকা সেশন অবজেক্ট ও লিমিটেড একাউন্ট ট্র্যাকার রিলিজ করা
        with _pool._condition:
            _pool._disabled = {}
            _pool._cred_idx = 0
            for e in _pool._pool:
                e['limit'] = False
                e['in_use'] = False
                e['verified'] = False
                try:
                    e['driver'].quit()
                except Exception:
                    pass
            _pool._pool = []
            
            # 💡 নতুন করে ব্রাউজার পুলের ট্র্যাকার রিসেট করা
            if hasattr(_pool, '_creds'):
                base = [c for c in get_credentials_list() if c.get('active', True)]
                _pool._creds = [c for c in base for _ in range(1)]
            
            _pool._condition.notify_all()
        
        # 🚨 অত্যন্ত গুরুত্বপূর্ণ: রিসেট শেষ হওয়ার সাথে সাথেই ব্যাকগ্রাউন্ড ব্রাউজার উইন্ডোগুলো পুনরায় চালু করা
        start_workers(n=2) 
        
        return jsonify({
            "success": True, 
            "message": "Selenium Browser Pool limits reset completely and fresh background instances triggered."
        })
    except Exception as e:
        print(f"Reset Error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ─── Cache control ────────────────────────────────────────────────────────────

@app.route('/api/cache', methods=['GET'])
def cache_status():
    files = [f for f in os.listdir(CACHE_DIR) if f.endswith('.json')]
    return jsonify({
        "enabled":    _cache_enabled,
        "file_count": len(files),
    })


@app.route('/api/cache/clear', methods=['POST'])
def cache_clear():
    key = request.args.get('key', '')
    if key != API_KEY:
        return jsonify({"success": False, "error": "Invalid API key"}), 401

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
    print("Starting Server — Selenium Browser Automation Mode")
    
    # 💡 ক্লাউড সার্ভার চালুর সময় ব্যাকগ্রাউন্ড ওয়ার্কার ও ব্রাউজার প্রি-ওয়ার্ম শুরু করা
    start_workers(n=2) 
    
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
