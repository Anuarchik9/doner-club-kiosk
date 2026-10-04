import json
import os
import socket
from pathlib import Path
from datetime import datetime

import requests
import urllib3
from flask import Flask, Response, jsonify, request, render_template_string
from dotenv import load_dotenv

load_dotenv()
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

SMART_POS_HOST = os.getenv("SMART_POS_HOST", "192.168.1.8").strip()
SMART_POS_PORT = int(os.getenv("SMART_POS_PORT", "8080"))
CASH_REGISTER_NAME = os.getenv("CASH_REGISTER_NAME", "DonerClubRepublic").strip()
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "0.0.0.0").strip()
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "8765"))
CLOUD_KIOSK_ORIGIN = os.getenv("CLOUD_KIOSK_ORIGIN", "https://kiosk.donerclub.kz").strip().rstrip("/")
KIOSK_BRIDGE_ORDER_TOKEN = os.getenv("KIOSK_BRIDGE_ORDER_TOKEN", "").strip()
BRIDGE_ALLOWED_ORIGINS = {
    origin.strip()
    for origin in os.getenv(
        "BRIDGE_ALLOWED_ORIGINS",
        "https://kiosk.donerclub.kz,https://doner-club-kiosk.onrender.com,http://127.0.0.1:8765,http://localhost:8765",
    ).split(",")
    if origin.strip()
}

DATA_DIR = Path(os.getenv(
    "KASPI_BRIDGE_DATA_DIR",
    Path(os.getenv("LOCALAPPDATA", ".")) / "DonerClubKaspiBridge"
))
DATA_DIR.mkdir(parents=True, exist_ok=True)
TOKEN_FILE = DATA_DIR / "tokens.json"

BASE_URL = f"https://{SMART_POS_HOST}:{SMART_POS_PORT}"
HTTP = requests.Session()
HTTP.verify = False

app = Flask(__name__, static_folder=None)

@app.after_request
def add_bridge_cors_headers(response):
    origin = request.headers.get("Origin")
    if origin and origin in BRIDGE_ALLOWED_ORIGINS:
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        # Chrome Private Network Access preflights use this header when a
        # public HTTPS kiosk page talks to the loopback bridge.
        response.headers["Access-Control-Allow-Private-Network"] = "true"
    return response

@app.route("/api/<path:_path>", methods=["OPTIONS"])
def bridge_preflight(_path):
    return ("", 204)

PAGE = """<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Doner Club Kaspi Bridge</title>
<style>
body{font-family:Arial,sans-serif;background:#f5f3ef;color:#161616;margin:0;padding:28px}
.wrap{max-width:760px;margin:auto}.card{background:#fff;border-radius:18px;padding:22px;box-shadow:0 8px 30px rgba(0,0,0,.06)}
h1{margin:0 0 8px}.muted{color:#777}.row{display:flex;gap:10px;flex-wrap:wrap;margin:18px 0}
button{border:0;border-radius:12px;padding:13px 16px;font-weight:700;cursor:pointer}
.primary{background:#ff5a12;color:#fff}.secondary{background:#efede9}.danger{background:#ffe9df;color:#9c3211}
pre{white-space:pre-wrap;background:#111;color:#eee;padding:14px;border-radius:12px;min-height:90px}
.ok{color:#168249}.bad{color:#b83222}
</style>
</head>
<body>
<div class="wrap"><div class="card">
<h1>Doner Club · Kaspi Bridge</h1>
<div class="muted">Локальный мост киоска Республики: Smart POS + облачный Kiosk + iiko.</div>
<p><b>Smart POS:</b> {{host}}:{{port}}<br><b>Имя кассы:</b> {{name}}<br><b>Локальный ПК:</b> {{local_ip}}</p>
<div class="row">
<button class="secondary" onclick="callApi('/api/ping','GET')">1. Проверить порт 8080</button>
<button class="primary" onclick="callApi('/api/register','POST')">2. Зарегистрировать кассу</button>
<button class="secondary" onclick="callApi('/api/device','GET')">3. Проверить Smart POS</button>
<button class="secondary" onclick="callApi('/api/refresh','POST')">Обновить токен</button>
</div>
<div id="hint" class="muted">Для регистрации нажмите кнопку 2 и подтвердите запрос на экране Smart POS.</div>
<pre id="out">Готово к проверке оборудования.</pre>
</div></div>
<script>
async function callApi(url,method){
  const out=document.getElementById('out');
  out.textContent='Выполняю запрос...';
  try{
    const r=await fetch(url,{method:method});
    const data=await r.json();
    out.textContent=JSON.stringify(data,null,2);
  }catch(e){out.textContent='Ошибка: '+e;}
}
</script>
</body></html>"""

def load_tokens():
    if not TOKEN_FILE.exists():
        return {}
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def save_tokens(data):
    TOKEN_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

def extract_data(payload):
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}

def smart_get(path, *, params=None, token=None, timeout=20):
    headers = {}
    if token:
        headers["accesstoken"] = token
    return HTTP.get(BASE_URL + path, params=params or {}, headers=headers, timeout=timeout)

def smart_device_info(access_token):
    response = smart_get("/v2/deviceinfo", token=access_token, timeout=20)
    payload = response.json()
    data = extract_data(payload)
    if not response.ok or payload.get("statusCode") != 0:
        raise RuntimeError(f"deviceinfo failed: {payload}")
    terminal_id = str(
        data.get("terminalId")
        or data.get("terminalID")
        or data.get("id")
        or ""
    ).strip()
    if not terminal_id:
        raise RuntimeError("Smart POS did not return terminalId")
    return payload, terminal_id

def local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((SMART_POS_HOST, SMART_POS_PORT))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "не определён"


def proxy_cloud_kiosk(path=None):
    target_path = path or request.path
    url = CLOUD_KIOSK_ORIGIN + target_path
    if request.query_string:
        url += "?" + request.query_string.decode("utf-8", errors="ignore")

    headers = {
        "Accept": request.headers.get("Accept", "*/*"),
        "User-Agent": "DonerClubRepublicBridge/1.0",
    }
    content_type = request.headers.get("Content-Type")
    if content_type:
        headers["Content-Type"] = content_type
    if target_path == "/kiosk-paid-order" and KIOSK_BRIDGE_ORDER_TOKEN:
        headers["X-Kiosk-Bridge-Token"] = KIOSK_BRIDGE_ORDER_TOKEN

    try:
        upstream = requests.request(
            request.method,
            url,
            headers=headers,
            data=request.get_data() if request.method in {"POST", "PUT", "PATCH"} else None,
            timeout=(8, 60),
            allow_redirects=False,
        )
    except requests.RequestException as error:
        return jsonify({
            "success": False,
            "code": "CLOUD_KIOSK_UNAVAILABLE",
            "message": "Не удалось связаться с сервером Doner Club.",
            "details": str(error),
        }), 502

    response_headers = {}
    for header in ("Content-Type", "Cache-Control", "ETag", "Last-Modified", "Location"):
        value = upstream.headers.get(header)
        if value:
            response_headers[header] = value
    return Response(upstream.content, status=upstream.status_code, headers=response_headers)


@app.get("/kiosk")
@app.get("/respublica")
@app.get("/Respublica")
def republic_kiosk():
    return proxy_cloud_kiosk("/respublica")


@app.get("/aray")
@app.get("/Aray")
def arai_kiosk():
    return proxy_cloud_kiosk("/aray")


@app.route("/static/<path:asset_path>", methods=["GET"])
def kiosk_static(asset_path):
    return proxy_cloud_kiosk("/static/" + asset_path)


@app.route("/kiosk-menu", methods=["GET"])
@app.route("/kiosk-stop-list", methods=["GET"])
@app.route("/kiosk-crm-register", methods=["POST"])
@app.route("/kiosk-crm-customer", methods=["GET"])
@app.route("/kiosk-paid-order", methods=["POST"])
@app.route("/kiosk-payment-readiness", methods=["GET"])
@app.route("/kiosk-iiko-config", methods=["GET"])
def kiosk_cloud_api_proxy():
    return proxy_cloud_kiosk(request.path)


@app.get("/")
def index():
    return render_template_string(
        PAGE,
        host=SMART_POS_HOST,
        port=SMART_POS_PORT,
        name=CASH_REGISTER_NAME,
        local_ip=local_ip(),
    )

@app.get("/api/health")
def health():
    tokens = load_tokens()
    return jsonify({
        "ok": True,
        "mode": "republic-kiosk",
        "paymentsEnabled": True,
        "cloudKiosk": CLOUD_KIOSK_ORIGIN,
        "localKioskUrl": f"http://{local_ip()}:{BRIDGE_PORT}/respublica",
        "smartPos": f"{SMART_POS_HOST}:{SMART_POS_PORT}",
        "cashRegisterName": CASH_REGISTER_NAME,
        "tokenStored": bool(tokens.get("accessToken")),
        "tokenFile": str(TOKEN_FILE),
        "localIp": local_ip(),
    })

@app.get("/api/ping")
def ping():
    try:
        with socket.create_connection((SMART_POS_HOST, SMART_POS_PORT), timeout=4):
            return jsonify({
                "ok": True,
                "message": "Smart POS доступен по TCP.",
                "smartPos": f"{SMART_POS_HOST}:{SMART_POS_PORT}",
                "localIp": local_ip(),
            })
    except Exception as error:
        return jsonify({
            "ok": False,
            "message": "Не удалось подключиться к Smart POS. Проверьте, что ПК и терминал в одной Wi‑Fi/LAN сети и IP терминала актуален.",
            "details": str(error),
            "smartPos": f"{SMART_POS_HOST}:{SMART_POS_PORT}",
            "localIp": local_ip(),
        }), 503

@app.post("/api/register")
def register():
    try:
        response = smart_get(
            "/v2/register",
            params={"name": CASH_REGISTER_NAME},
            timeout=120,
        )
        payload = response.json()
        data = extract_data(payload)
        if response.ok and payload.get("statusCode") == 0 and data.get("accessToken"):
            save_tokens({
                "name": CASH_REGISTER_NAME,
                "accessToken": data.get("accessToken"),
                "refreshToken": data.get("refreshToken"),
                "expirationDate": data.get("expirationDate"),
                "smartPosHost": SMART_POS_HOST,
                "savedAt": datetime.now().isoformat(timespec="seconds"),
            })
            return jsonify({
                "ok": True,
                "message": "Касса зарегистрирована. Токены сохранены локально.",
                "expirationDate": data.get("expirationDate"),
                "tokenFile": str(TOKEN_FILE),
            })
        return jsonify({
            "ok": False,
            "message": "Smart POS не завершил регистрацию.",
            "httpStatus": response.status_code,
            "response": payload,
        }), 400
    except requests.Timeout:
        return jsonify({
            "ok": False,
            "message": "Smart POS не ответил вовремя. Если на терминале был запрос доступа — повторите регистрацию и нажмите «Разрешить».",
        }), 504
    except Exception as error:
        return jsonify({"ok": False, "message": "Ошибка регистрации.", "details": str(error)}), 500

@app.post("/api/refresh")
def refresh():
    tokens = load_tokens()
    refresh_token = tokens.get("refreshToken")
    name = tokens.get("name") or CASH_REGISTER_NAME
    if not refresh_token:
        return jsonify({"ok": False, "message": "Сначала зарегистрируйте кассу."}), 400
    try:
        response = smart_get(
            "/v2/revoke",
            params={"name": name, "refreshToken": refresh_token},
            timeout=30,
        )
        payload = response.json()
        data = extract_data(payload)
        if response.ok and payload.get("statusCode") == 0 and data.get("accessToken"):
            save_tokens({
                "name": name,
                "accessToken": data.get("accessToken"),
                "refreshToken": data.get("refreshToken"),
                "expirationDate": data.get("expirationDate"),
                "smartPosHost": SMART_POS_HOST,
                "savedAt": datetime.now().isoformat(timespec="seconds"),
            })
            return jsonify({"ok": True, "message": "Токен обновлён.", "expirationDate": data.get("expirationDate")})
        return jsonify({"ok": False, "response": payload}), 400
    except Exception as error:
        return jsonify({"ok": False, "message": "Ошибка обновления токена.", "details": str(error)}), 500

@app.get("/api/device")
def device():
    tokens = load_tokens()
    access_token = tokens.get("accessToken")
    if not access_token:
        return jsonify({"ok": False, "message": "Сначала зарегистрируйте кассу."}), 401
    try:
        payload, terminal_id = smart_device_info(access_token)
        tokens["terminalId"] = terminal_id
        save_tokens(tokens)
        return jsonify({
            "ok": True,
            "terminalId": terminal_id,
            "response": payload,
        })
    except Exception as error:
        return jsonify({"ok": False, "message": "Ошибка запроса deviceinfo.", "details": str(error)}), 500

@app.post("/api/payment/start")
def payment_start():
    tokens = load_tokens()
    access_token = tokens.get("accessToken")
    if not access_token:
        return jsonify({"ok": False, "message": "Сначала зарегистрируйте кассу."}), 401

    body = request.get_json(silent=True) or {}
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Сумма должна быть целым числом тенге."}), 400

    if amount <= 0:
        return jsonify({"ok": False, "message": "Сумма должна быть больше 0 ₸."}), 400
    if amount > 500000:
        return jsonify({"ok": False, "message": "Лимит одной операции: 500 000 ₸."}), 400

    try:
        payload, terminal_id = smart_device_info(access_token)
        tokens["terminalId"] = terminal_id
        save_tokens(tokens)

        response = smart_get(
            "/v2/payment",
            params={"amount": amount, "owncheque": "true"},
            token=access_token,
            timeout=30,
        )
        payment_payload = response.json()
        data = extract_data(payment_payload)
        process_id = str(data.get("processId") or "").strip()
        if response.ok and payment_payload.get("statusCode") == 0 and process_id:
            return jsonify({
                "ok": True,
                "amount": amount,
                "processId": process_id,
                "status": data.get("status"),
                "terminalId": terminal_id,
                "message": "Оплата запущена на Smart POS.",
            })
        return jsonify({
            "ok": False,
            "message": "Smart POS не запустил оплату.",
            "response": payment_payload,
        }), 400
    except Exception as error:
        return jsonify({"ok": False, "message": "Ошибка запуска оплаты.", "details": str(error)}), 500

@app.get("/api/payment/status")
def payment_status():
    tokens = load_tokens()
    access_token = tokens.get("accessToken")
    if not access_token:
        return jsonify({"ok": False, "message": "Сначала зарегистрируйте кассу."}), 401

    process_id = str(request.args.get("processId") or "").strip()
    if not process_id:
        return jsonify({"ok": False, "message": "Не передан processId."}), 400

    try:
        terminal_id = str(tokens.get("terminalId") or "").strip()
        if not terminal_id:
            _, terminal_id = smart_device_info(access_token)
            tokens["terminalId"] = terminal_id
            save_tokens(tokens)

        response = HTTP.get(
            BASE_URL + "/v2/status",
            params={"processId": process_id},
            headers={"accesstoken": access_token, "terminalId": terminal_id},
            timeout=20,
        )
        payload = response.json()
        data = extract_data(payload)
        return jsonify({
            "ok": response.ok and payload.get("statusCode") == 0,
            "processId": process_id,
            "status": data.get("status"),
            "subStatus": data.get("subStatus"),
            "message": data.get("message"),
            "response": payload,
        }), response.status_code
    except Exception as error:
        return jsonify({"ok": False, "message": "Ошибка запроса статуса оплаты.", "details": str(error)}), 500

if __name__ == "__main__":
    print("=" * 64)
    print("Doner Club Republic Kiosk Bridge")
    print(f"Smart POS: {BASE_URL}")
    print(f"Cash register name: {CASH_REGISTER_NAME}")
    print(f"Setup on this PC: http://127.0.0.1:{BRIDGE_PORT}")
    print(f"Kiosk on iPad: http://{local_ip()}:{BRIDGE_PORT}/respublica")
    print("Keep this window open while the kiosk is operating.")
    print("=" * 64)
    app.run(host=BRIDGE_HOST, port=BRIDGE_PORT, debug=False)
