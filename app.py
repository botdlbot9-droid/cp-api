import os
import re
import time
import requests
import logging
import glob
from xml.etree import ElementTree as ET
from flask import Flask, request, jsonify
from flask_cors import CORS

# Pywidevine for DRM
try:
    from pywidevine.pssh import PSSH
    from pywidevine.cdm import Cdm
    from pywidevine.device import Device
    PYWIDEVINE_AVAILABLE = True
except ImportError:
    PYWIDEVINE_AVAILABLE = False
    logging.warning("pywidevine not installed")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# ============================================================
#  CONFIG — Environment Variables se le raha hai
# ============================================================
CLASSPLUS_TOKEN = os.getenv("CLASSPLUS_TOKEN", "")
CURRENT_DEVICE_ID = os.getenv("DEVICE_ID", "a1b2c3d4e5f67890")

# ============================================================
#  WVD FILE FINDER
# ============================================================
def find_wvd_file():
    paths = [
        'WVDs/*.wvd',
        './WVDs/*.wvd',
        'WVDs/device.wvd',
        './WVDs/device.wvd',
        '*.wvd'
    ]
    for pattern in paths:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    raise FileNotFoundError("No .wvd file found in WVDs/ folder")

# ============================================================
#  MAIN SIGN FUNCTION
# ============================================================
def sign_url(url, token):
    if not token:
        return {"error": "No token provided. Set CLASSPLUS_TOKEN environment variable."}

    headers = {
        'accept': 'application/json, text/plain, */*',
        'accept-encoding': 'gzip',
        'accept-language': 'EN',
        'api-version': '35',
        'app-version': '1.4.73.2',
        'build-number': '35',
        'connection': 'Keep-Alive',
        'content-type': 'application/json',
        'device-details': 'Xiaomi_Redmi 7_SDK-32',
        'device-id': CURRENT_DEVICE_ID,
        'host': 'api.classplusapp.com',
        'region': 'IN',
        'user-agent': 'Mobile-Android',
        'webengage-luid': '00000187-6fe4-5d41-a530-26186858be4c',
        'x-access-token': token
    }

    try:
        # Extract contentId from URL
        content_id = None
        if 'akamai-cdn.classplusapp.com' in url:
            match = re.search(r'/lc/([^/]+)/', url)
            if match:
                content_id = match.group(1)
        elif 'contentHashId=' in url:
            content_id = url.split('contentHashId=')[1].split('&')[0]

        # Call ClassPlus API
        if content_id:
            resp = requests.get(
                f'https://api.classplusapp.com/cams/uploader/video/jw-signed-url?contentId={content_id}&offlineDownload=false',
                headers=headers, timeout=15
            )
        else:
            resp = requests.get(
                f'https://api.classplusapp.com/cams/uploader/video/jw-signed-url?url={url}',
                headers=headers, timeout=15
            )

        data = resp.json()
        logger.info(f"ClassPlus API response: {data.get('status', 'unknown')}")

        # Non-DRM case
        if data.get('status') == 'ok' and data.get('url'):
            return {"url": data['url']}

        # Token invalid
        if data.get('error') == 'Invalid token' or data.get('status') == 'failure':
            return {"error": "Token expired or invalid"}

        # DRM case
        drm_urls = data.get('drmUrls')
        if not drm_urls:
            return {"error": "No DRM and no direct URL"}

        mpd_url = drm_urls.get('manifestUrl')
        lic_url = drm_urls.get('licenseUrl')
        if not mpd_url or not lic_url:
            return {"error": "Missing DRM manifest or license URL"}

        # Fetch MPD
        mpd_resp = requests.get(mpd_url, timeout=10)
        if mpd_resp.status_code != 200:
            return {"error": f"MPD fetch failed: HTTP {mpd_resp.status_code}"}

        # Extract PSSH
        pssh_b64 = None
        try:
            root = ET.fromstring(mpd_resp.content)
            for elem in root.iter():
                if 'ContentProtection' in elem.tag:
                    pssh_elem = elem.find('.//pssh')
                    if pssh_elem is None:
                        pssh_elem = elem.find('.//{urn:mpeg:cenc:2013}pssh')
                    if pssh_elem is not None and pssh_elem.text:
                        pssh_b64 = pssh_elem.text.strip()
                        break
        except ET.ParseError:
            return {"error": "Invalid MPD XML"}

        if not pssh_b64:
            return {"error": "PSSH not found in MPD"}

        if not PYWIDEVINE_AVAILABLE:
            return {"error": "Pywidevine not installed"}

        # Decrypt with Widevine
        try:
            wvd_path = find_wvd_file()
            logger.info(f"Loading WVD: {wvd_path}")

            device = Device.load(wvd_path)
            cdm = Cdm.from_device(device)

            pssh = PSSH(pssh_b64)
            session_id = cdm.open()
            challenge = cdm.get_license_challenge(session_id, pssh)

            lic_headers = {
                'user-agent': 'okhttp/4.9.3',
                'content-type': 'application/octet-stream'
            }
            lic_resp = requests.post(lic_url, data=challenge, headers=lic_headers, timeout=15)

            if lic_resp.status_code != 200:
                cdm.close(session_id)
                return {"error": f"License request failed: {lic_resp.status_code}"}

            cdm.parse_license(session_id, lic_resp.content)
            keys = []
            for key in cdm.get_keys(session_id):
                if key.type == 'CONTENT':
                    keys.append(f"{key.kid.hex}:{key.key.hex()}")

            cdm.close(session_id)

            if not keys:
                return {"error": "No decryption keys extracted"}

            logger.info(f"✅ Extracted {len(keys)} keys")
            return {"MPD": mpd_url, "KEYS": keys}

        except FileNotFoundError as e:
            return {"error": f"WVD file error: {str(e)}"}
        except Exception as e:
            logger.error(f"Decryption error: {e}")
            return {"error": f"DRM decryption failed: {str(e)}"}

    except Exception as e:
        logger.error(f"sign_url error: {e}")
        return {"error": str(e)}

# ============================================================
#  ROUTES
# ============================================================
@app.route('/', methods=['GET'])
def home():
    token_status = "✅ Set" if CLASSPLUS_TOKEN else "❌ Not set"
    return jsonify({
        "status": "✅ ClassPlus DRM Proxy API is running",
        "endpoint": "/itsgolu?url=YOUR_URL",
        "token_status": token_status,
        "docs": "Set CLASSPLUS_TOKEN environment variable"
    })

@app.route('/itsgolu', methods=['GET'])
def itsgolu():
    url = request.args.get('url')
    if not url:
        return jsonify({"error": "url parameter required"}), 400

    if not CLASSPLUS_TOKEN:
        return jsonify({
            "success": False,
            "error": "CLASSPLUS_TOKEN not set. Please set environment variable."
        }), 500

    logger.info(f"Processing: {url[:80]}...")
    result = sign_url(url, CLASSPLUS_TOKEN)

    if "error" in result:
        return jsonify({"success": False, "error": result["error"]}), 500

    return jsonify({"success": True, **result})

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "status": "healthy",
        "pywidevine": "✅ Available" if PYWIDEVINE_AVAILABLE else "❌ Not installed",
        "wvd": "✅ Found" if glob.glob('WVDs/*.wvd') else "❌ Not found"
    })

# ============================================================
#  RUN
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
