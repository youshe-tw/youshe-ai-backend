import os, base64, time, json, sqlite3, secrets, hmac, hashlib, uuid, io
from pathlib import Path
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from pydantic import BaseModel
import httpx
import qrcode

load_dotenv()

from providers import gemini, openai_provider

app = FastAPI(title="有舍空間設計 AI 生圖 Backend", version="4.5.0")
_allowed_origins_raw = os.getenv("YUSHE_ALLOWED_ORIGINS", "http://127.0.0.1:8000,http://localhost:8000")
_allowed_origins = [x.strip() for x in _allowed_origins_raw.split(",") if x.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=False,
    allow_methods=["GET","POST","OPTIONS"],
    allow_headers=["Content-Type"],
)

OUT = Path(__file__).parent / "storage"
OUT.mkdir(exist_ok=True)
app.mount("/storage", StaticFiles(directory=str(OUT)), name="storage")


DB_PATH = Path(__file__).parent / "yushe_trial.db"
RENDER_COST = int(os.getenv("YUSHE_RENDER_COST", "100"))
NEW_USER_BONUS = int(os.getenv("YUSHE_NEW_USER_BONUS", "500"))
PAYMENT_MODE = os.getenv("YUSHE_PAYMENT_MODE", "production").strip().lower()
LINE_PAY_CHANNEL_ID = os.getenv("LINE_PAY_CHANNEL_ID", "").strip()
LINE_PAY_CHANNEL_SECRET = os.getenv("LINE_PAY_CHANNEL_SECRET", "").strip()
LINE_PAY_API_BASE = os.getenv("LINE_PAY_API_BASE", "https://api-pay.line.me").rstrip("/")
LINE_PAY_CONFIRM_URL = os.getenv("LINE_PAY_CONFIRM_URL", "http://127.0.0.1:8000/billing/linepay/return").strip()
LINE_PAY_CANCEL_URL = os.getenv("LINE_PAY_CANCEL_URL", "http://127.0.0.1:8000/billing/linepay/cancel").strip()
TOPUP_PLANS = {
    "test50": {"name":"測試儲值", "amount":50, "points":1000},
    "starter250": {"name":"嚐鮮會員", "amount":250, "points":5100},
    "pro500": {"name":"專業會員", "amount":500, "points":10100},
    "max1000": {"name":"暢享會員", "amount":1000, "points":20200},
}

def db_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_trial_db():
    conn = db_conn()
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            account_type TEXT NOT NULL,
            account_id TEXT NOT NULL UNIQUE,
            points INTEGER NOT NULL DEFAULT 0,
            trial_granted INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions(
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS point_transactions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            points INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            job_id TEXT,
            note TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS render_jobs(
            job_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            provider TEXT NOT NULL,
            cost INTEGER NOT NULL,
            status TEXT NOT NULL,
            result_path TEXT,
            created_at INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS purchase_orders(
            order_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            plan_id TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            amount INTEGER NOT NULL,
            points INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'created',
            payment_method TEXT NOT NULL DEFAULT 'line_pay_qr',
            created_at INTEGER NOT NULL,
            paid_at INTEGER,
            external_txn_id TEXT UNIQUE,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """)
        # v4.3.1 migration for databases already created by v4.3.0
        try:
            conn.execute("ALTER TABLE purchase_orders ADD COLUMN external_txn_id TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_purchase_orders_external_txn ON purchase_orders(external_txn_id) WHERE external_txn_id IS NOT NULL")
        except sqlite3.OperationalError:
            pass
        for col, typ in [("payment_url","TEXT"),("linepay_status","TEXT"),("linepay_message","TEXT")]:
            try:
                conn.execute(f"ALTER TABLE purchase_orders ADD COLUMN {col} {typ}")
            except sqlite3.OperationalError:
                pass
    finally:
        conn.close()

init_trial_db()

class LoginRequest(BaseModel):
    account_type: str = "email"
    account_id: str

def user_from_token(token: str):
    if not token:
        return None
    conn = db_conn()
    try:
        return conn.execute("""
            SELECT u.* FROM sessions s
            JOIN users u ON u.id=s.user_id
            WHERE s.token=? AND u.status='active'
        """, (token,)).fetchone()
    finally:
        conn.close()

def trial_status_payload(user):
    pts = int(user["points"])
    return {
        "logged_in": True,
        "user_id": int(user["id"]),
        "account_id": user["account_id"],
        "account_type": user["account_type"],
        "points": pts,
        "render_cost": RENDER_COST,
        "trial_total": NEW_USER_BONUS,
        "remaining_renders": max(0, pts // RENDER_COST),
        "trial_renders_total": max(1, NEW_USER_BONUS // RENDER_COST)
    }

@app.post("/auth/login")
def auth_login(req: LoginRequest):
    account_id = req.account_id.strip().lower()
    account_type = (req.account_type or "email").strip().lower()
    if not account_id or len(account_id) > 200:
        raise HTTPException(status_code=400, detail="請輸入有效的 Email / 帳號。")

    conn = db_conn()
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE account_id=?", (account_id,)).fetchone()
        if not user:
            conn.execute(
                "INSERT INTO users(account_type,account_id,points,trial_granted,status,created_at) VALUES(?,?,?,?,?,?)",
                (account_type, account_id, NEW_USER_BONUS, 1, "active", now)
            )
            uid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute(
                "INSERT INTO point_transactions(user_id,type,points,balance_after,job_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (uid, "trial_bonus", NEW_USER_BONUS, NEW_USER_BONUS, None, "新會員免費體驗", now)
            )
            user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

        token = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions(token,user_id,created_at) VALUES(?,?,?)", (token, user["id"], now))
        conn.commit()
        return {"status":"success", "access_token":token, "user":trial_status_payload(user)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class TopupOrderRequest(BaseModel):
    token: str
    plan_id: str

class TopupConfirmRequest(BaseModel):
    token: str
    order_id: str

class ManualPaidRecoveryRequest(BaseModel):
    token: str
    transaction_id: str
    amount: int = 50

class PaymentCallbackRequest(BaseModel):
    order_id: str
    transaction_id: str
    amount: int
    secret: str


def mark_order_paid_atomic(order_id: str, transaction_id: str, expected_amount=None):
    txn = "".join(ch for ch in (transaction_id or "").strip() if ch.isalnum())
    if len(txn) < 8:
        raise HTTPException(status_code=400, detail="INVALID_TRANSACTION_ID")

    conn = db_conn()
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("SELECT * FROM purchase_orders WHERE order_id=?", (order_id,)).fetchone()
        if not order:
            conn.rollback()
            raise HTTPException(status_code=404, detail="ORDER_NOT_FOUND")

        if expected_amount is not None and int(order["amount"]) != int(expected_amount):
            conn.rollback()
            raise HTTPException(status_code=409, detail="PAYMENT_AMOUNT_MISMATCH")

        if order["status"] == "paid":
            balance = conn.execute("SELECT points FROM users WHERE id=?", (order["user_id"],)).fetchone()["points"]
            conn.commit()
            return {"status":"paid","order_id":order_id,"points_added":0,
                    "remaining_points":int(balance),"already_paid":True}

        used = conn.execute(
            "SELECT order_id FROM purchase_orders WHERE external_txn_id=? AND order_id<>?",
            (txn, order_id)
        ).fetchone()
        if used:
            conn.rollback()
            raise HTTPException(status_code=409, detail="TRANSACTION_ALREADY_USED")

        conn.execute(
            "UPDATE purchase_orders SET status='paid', paid_at=?, external_txn_id=? WHERE order_id=?",
            (now, txn, order_id)
        )
        conn.execute("UPDATE users SET points=points+? WHERE id=?", (order["points"], order["user_id"]))
        balance = conn.execute("SELECT points FROM users WHERE id=?", (order["user_id"],)).fetchone()["points"]
        conn.execute(
            "INSERT INTO point_transactions(user_id,type,points,balance_after,job_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (order["user_id"],"recharge",order["points"],balance,order_id,
             f"付款確認自動入點｜交易 {txn}",now)
        )
        conn.commit()
        return {"status":"paid","order_id":order_id,"points_added":int(order["points"]),
                "remaining_points":int(balance),"already_paid":False}
    except HTTPException:
        raise
    except sqlite3.IntegrityError:
        conn.rollback()
        raise HTTPException(status_code=409, detail="TRANSACTION_ALREADY_USED")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def _linepay_ready():
    return bool(LINE_PAY_CHANNEL_ID and LINE_PAY_CHANNEL_SECRET)

def _linepay_json(data):
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))

def _linepay_headers(method, api_path, body_text="", query_string=""):
    nonce = str(uuid.uuid4())
    if method.upper() == "GET":
        msg = LINE_PAY_CHANNEL_SECRET + api_path + query_string + nonce
    else:
        msg = LINE_PAY_CHANNEL_SECRET + api_path + body_text + nonce
    sig = base64.b64encode(hmac.new(LINE_PAY_CHANNEL_SECRET.encode(), msg.encode(), hashlib.sha256).digest()).decode()
    return {"Content-Type":"application/json","X-LINE-ChannelId":LINE_PAY_CHANNEL_ID,
            "X-LINE-Authorization-Nonce":nonce,"X-LINE-Authorization":sig}

async def _linepay_call(method, api_path, data=None, query_string="", timeout=45):
    if not _linepay_ready():
        raise HTTPException(503, "LINE_PAY_NOT_CONFIGURED")
    body = _linepay_json(data) if data is not None else ""
    headers = _linepay_headers(method, api_path, body, query_string)
    url = LINE_PAY_API_BASE + api_path + query_string
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.request(method, url, headers=headers, content=body if data is not None else None)
        # LINE Pay transactionId can exceed JS safe integer; Python handles it safely.
        d = r.json()
    except Exception as e:
        raise HTTPException(502, f"LINE_PAY_NETWORK_ERROR: {e}")
    return d

def _qr_data_url(text):
    img=qrcode.make(text)
    b=io.BytesIO(); img.save(b, format="PNG")
    return "data:image/png;base64,"+base64.b64encode(b.getvalue()).decode()

@app.get("/billing/health")
def billing_health():
    return {"ok": True, "version": "4.5.0", "payment_mode": PAYMENT_MODE,
            "line_pay_configured": _linepay_ready(), "line_pay_api_base": LINE_PAY_API_BASE,
            "supports_real_payment": True, "supports_auto_credit": True}

@app.get("/billing/plans")
def billing_plans():
    return {"payment_mode":PAYMENT_MODE,"payment_method":"LINE Pay Online API v4","plans":TOPUP_PLANS}

@app.post("/billing/create_order")
async def billing_create_order(req: TopupOrderRequest):
    user = user_from_token(req.token)
    if not user: raise HTTPException(401,"LOGIN_REQUIRED")
    plan=TOPUP_PLANS.get(req.plan_id)
    if not plan: raise HTTPException(400,"INVALID_PLAN")
    if not _linepay_ready(): raise HTTPException(503,"LINE_PAY_NOT_CONFIGURED")
    order_id="YS"+time.strftime("%Y%m%d%H%M%S")+secrets.token_hex(3).upper()
    now=int(time.time())
    conn=db_conn()
    try:
        conn.execute("INSERT INTO purchase_orders(order_id,user_id,plan_id,plan_name,amount,points,status,payment_method,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
          (order_id,user["id"],req.plan_id,plan["name"],plan["amount"],plan["points"],"created","line_pay_v4",now))
    finally: conn.close()
    payload={"amount":plan["amount"],"currency":"TWD","orderId":order_id,
      "packages":[{"id":"YUSHE-CREDITS","amount":plan["amount"],"products":[{"id":req.plan_id,"name":f"有舍 AI 點數｜{plan['name']}","quantity":1,"price":plan["amount"]}]}],
      "redirectUrls":{"confirmUrl":LINE_PAY_CONFIRM_URL+f"?order_id={order_id}","cancelUrl":LINE_PAY_CANCEL_URL+f"?order_id={order_id}"}}
    d=await _linepay_call("POST","/v4/payments/request",payload,timeout=15)
    if d.get("returnCode")!="0000":
        conn=db_conn(); conn.execute("UPDATE purchase_orders SET status='failed',linepay_status=?,linepay_message=? WHERE order_id=?",(d.get("returnCode"),d.get("returnMessage"),order_id)); conn.close()
        raise HTTPException(502,f"LINE_PAY_REQUEST_FAILED {d.get('returnCode')}: {d.get('returnMessage')}")
    info=d.get("info") or {}; txn=str(info.get("transactionId") or "")
    purl=(info.get("paymentUrl") or {}).get("universal") or (info.get("paymentUrl") or {}).get("web")
    if not txn or not purl: raise HTTPException(502,"LINE_PAY_INVALID_RESPONSE")
    conn=db_conn()
    try: conn.execute("UPDATE purchase_orders SET status='payment_requested',external_txn_id=?,payment_url=?,linepay_status='0000' WHERE order_id=?",(txn,purl,order_id))
    finally: conn.close()
    return {"order_id":order_id,"plan_id":req.plan_id,"plan_name":plan["name"],"amount":plan["amount"],"points":plan["points"],
            "transaction_id":txn,"payment_url":purl,"payment_qr":_qr_data_url(purl),"payment_mode":"production"}

async def _sync_linepay_order(order, user_id=None):
    if order["status"]=="paid":
        conn=db_conn(); bal=conn.execute("SELECT points FROM users WHERE id=?",(order["user_id"],)).fetchone()["points"]; conn.close()
        return {"status":"paid","remaining_points":bal,"points_added":0,"already_paid":True}
    txn=str(order["external_txn_id"] or "")
    if not txn: return {"status":order["status"]}
    d=await _linepay_call("GET",f"/v4/payments/requests/{txn}/check",timeout=25)
    code=str(d.get("returnCode") or "")
    conn=db_conn()
    try: conn.execute("UPDATE purchase_orders SET linepay_status=?,linepay_message=? WHERE order_id=?",(code,d.get("returnMessage"),order["order_id"]))
    finally: conn.close()
    if code=="0110":
        cd=await _linepay_call("POST",f"/v4/payments/{txn}/confirm",{"amount":int(order["amount"]),"currency":"TWD"},timeout=45)
        if cd.get("returnCode")!="0000":
            raise HTTPException(502,f"LINE_PAY_CONFIRM_FAILED {cd.get('returnCode')}: {cd.get('returnMessage')}")
        return mark_order_paid_atomic(order["order_id"],txn,int(order["amount"]))
    if code=="0123":
        # LINE Pay reports completed; idempotently credit the exact stored order amount.
        return mark_order_paid_atomic(order["order_id"],txn,int(order["amount"]))
    if code in ("0121","0122","1180"):
        conn=db_conn(); conn.execute("UPDATE purchase_orders SET status='cancelled' WHERE order_id=?",(order["order_id"],)); conn.close()
        return {"status":"cancelled","linepay_code":code}
    return {"status":"waiting","linepay_code":code,"linepay_message":d.get("returnMessage")}

@app.get("/billing/order_status")
async def billing_order_status(token: str="", order_id: str=""):
    user=user_from_token(token)
    if not user: raise HTTPException(401,"LOGIN_REQUIRED")
    conn=db_conn()
    try: order=conn.execute("SELECT * FROM purchase_orders WHERE order_id=? AND user_id=?",(order_id,user["id"])).fetchone()
    finally: conn.close()
    if not order: raise HTTPException(404,"ORDER_NOT_FOUND")
    result=await _sync_linepay_order(order,user["id"])
    result.update({"order_id":order_id,"plan_name":order["plan_name"],"amount":order["amount"],"points":order["points"]})
    return result

@app.get("/billing/linepay/return")
async def linepay_return(order_id: str="", transactionId: str=""):
    # Browser return is only a convenience. Real credit is server-side and idempotent.
    return {"ok":True,"message":"付款授權完成，請回到 SketchUp；系統將自動確認並入點。","order_id":order_id}

@app.get("/billing/linepay/cancel")
def linepay_cancel(order_id: str=""):
    return {"ok":False,"message":"付款已取消，可回到 SketchUp 重新建立訂單。","order_id":order_id}

@app.post("/billing/confirm_test_payment")
def billing_confirm_test_payment_disabled(req: TopupConfirmRequest):
    raise HTTPException(410,"TEST_CREDIT_DISABLED_IN_PRODUCTION")

@app.post("/billing/recover_paid_test")
def billing_recover_paid_test_disabled(req: ManualPaidRecoveryRequest):
    raise HTTPException(410,"MANUAL_UNVERIFIED_CREDIT_DISABLED_IN_PRODUCTION")

@app.get("/billing/orders")
def billing_orders(token: str="", limit: int=50):
    user=user_from_token(token)
    if not user: raise HTTPException(401,"LOGIN_REQUIRED")
    conn=db_conn()
    try:
        rows=conn.execute("SELECT order_id,plan_id,plan_name,amount,points,status,payment_method,created_at,paid_at,external_txn_id FROM purchase_orders WHERE user_id=? ORDER BY created_at DESC LIMIT ?",(user["id"],max(1,min(limit,100)))).fetchall()
        return {"items":[dict(r) for r in rows],"payment_mode":"production"}
    finally: conn.close()

@app.get("/trial/status")
def trial_status(token: str = ""):
    user = user_from_token(token)
    if not user:
        return {
            "logged_in": False,
            "points": 0,
            "render_cost": RENDER_COST,
            "trial_total": NEW_USER_BONUS,
            "remaining_renders": 0,
            "trial_renders_total": max(1, NEW_USER_BONUS // RENDER_COST)
        }
    return trial_status_payload(user)

@app.get("/trial/transactions")
def trial_transactions(token: str = "", limit: int = 50):
    user = user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="LOGIN_REQUIRED")
    conn = db_conn()
    try:
        rows = conn.execute("""
            SELECT type,points,balance_after,job_id,note,created_at
            FROM point_transactions WHERE user_id=?
            ORDER BY id DESC LIMIT ?
        """, (user["id"], max(1,min(limit,100)))).fetchall()
        return {"items":[dict(r) for r in rows]}
    finally:
        conn.close()

def reserve_render_points(token: str, provider: str):
    user = user_from_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="LOGIN_REQUIRED")

    job_id = "job_" + secrets.token_hex(12)
    conn = db_conn()
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE users SET points = points - ? WHERE id=? AND points >= ? AND status='active'",
            (RENDER_COST, user["id"], RENDER_COST)
        )
        if cur.rowcount != 1:
            conn.rollback()
            raise HTTPException(status_code=402, detail="FREE_TRIAL_EXHAUSTED")

        balance = conn.execute("SELECT points FROM users WHERE id=?", (user["id"],)).fetchone()["points"]
        conn.execute(
            "INSERT INTO render_jobs(job_id,user_id,provider,cost,status,result_path,created_at) VALUES(?,?,?,?,?,?,?)",
            (job_id, user["id"], provider, RENDER_COST, "processing", None, now)
        )
        conn.execute(
            "INSERT INTO point_transactions(user_id,type,points,balance_after,job_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
            (user["id"], "render_charge", -RENDER_COST, balance, job_id, "AI 渲染預扣", now)
        )
        conn.commit()
        return int(user["id"]), job_id, int(balance)
    except HTTPException:
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def refund_render_points(user_id: int, job_id: str, reason: str):
    conn = db_conn()
    now = int(time.time())
    try:
        conn.execute("BEGIN IMMEDIATE")
        job = conn.execute("SELECT * FROM render_jobs WHERE job_id=? AND user_id=?", (job_id,user_id)).fetchone()
        if job and job["status"] == "processing":
            conn.execute("UPDATE users SET points=points+? WHERE id=?", (RENDER_COST,user_id))
            balance = conn.execute("SELECT points FROM users WHERE id=?", (user_id,)).fetchone()["points"]
            conn.execute("UPDATE render_jobs SET status='refunded' WHERE job_id=?", (job_id,))
            conn.execute(
                "INSERT INTO point_transactions(user_id,type,points,balance_after,job_id,note,created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id,"render_refund",RENDER_COST,balance,job_id,reason[:300],now)
            )
            conn.commit()
    finally:
        conn.close()

def complete_render_job(user_id: int, job_id: str, result_path: str):
    conn = db_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE render_jobs SET status='success', result_path=? WHERE job_id=? AND user_id=?",
            (result_path,job_id,user_id)
        )
        conn.commit()
    finally:
        conn.close()

PRESETS = {
    "cream":"warm contemporary cream interior, ivory mineral plaster with subtle micro-texture, light natural oak veneer with realistic grain, refined woven textiles, soft warm indirect lighting, restrained styling",
    "minimal":"contemporary minimalist interior, restrained neutral palette, precise joinery, realistic matte paint, natural timber, clean material transitions, disciplined detailing",
    "wabi":"warm wabi-sabi interior, tactile mineral plaster, natural timber, linen and handmade surfaces, subtle imperfections, soft diffused daylight, calm refined atmosphere",
    "japanese":"Japanese natural contemporary interior, pale timber with visible natural grain, warm white plaster, restrained joinery, paper/linen textures, calm daylight",
    "luxury":"understated contemporary luxury interior, refined natural stone, premium timber veneer, subtle brushed metal accents, layered architectural lighting, sophisticated textiles",
    "custom":""
}

PHOTO_QUALITY = {
    "standard": "Professional architectural visualization with realistic materials and balanced lighting.",
    "photo": """PHOTO-REAL ARCHITECTURAL PHOTOGRAPHY MODE. Make the result read as a real interior photograph rather than CGI: physically plausible PBR materials, correct roughness and micro-surface variation, natural wood grain scale, realistic stone/plaster pores, believable fabric weave, accurate glass and metal reflections, global illumination, soft contact shadows, ambient occlusion only where physically plausible, natural highlight roll-off, realistic exposure and white balance, subtle lens response, clean verticals, high dynamic range, fine photographic detail. Avoid plastic surfaces, flat textures, over-smoothed materials, excessive bloom, fake glow, oversaturation, crunchy sharpening and obvious AI artifacts.""",
    "ultra": """ULTRA PHOTO-REAL INTERIOR PHOTOGRAPHY. Editorial architecture magazine quality, physically based material response, ray-traced-looking global illumination, nuanced bounced light, accurate contact shadows, realistic specular/reflection behavior, micro-texture and imperfections, true-to-life fabric/wood/stone/glass/metal, natural camera exposure, neutral cinematic color science, realistic depth cues, crisp but non-CGI detail. The image must be indistinguishable from a professionally photographed completed interior. No plastic CGI look, no fantasy lighting, no exaggerated bloom, no warped geometry, no duplicate objects, no AI artifacts."""
}

STRUCTURE = {
    "strict": """ABSOLUTE PRIORITY — GEOMETRY LOCK:
Use the first image (the SketchUp source) as the authoritative geometry reference.
Preserve the exact camera viewpoint, perspective, field of view, room proportions, walls, columns, doors, windows, openings, ceiling outlines, cabinetry boundaries, furniture footprint, circulation and major object positions.
Do NOT add, remove, resize, relocate, rotate or redesign architectural elements.
Do NOT invent new doors, windows, cabinets, wall panels, ceiling features or furniture.
Only improve materials, texture realism, lighting, reflections, fabric realism, small decor and photographic realism.
If any style reference conflicts with the SketchUp source geometry, always follow the SketchUp source geometry.""",
    "balanced": """Strongly preserve the source camera, room envelope, openings, cabinetry, ceiling and furniture layout. Style references control materials, color palette, lighting and atmosphere only. Minor decorative refinement is allowed.""",
    "creative": """Use the SketchUp source as the primary spatial reference, preserving the camera and main envelope, while allowing tasteful design refinement."""
}

@app.get("/health")
def health():
    return {
        "ok": True,
        "version": "4.5.0",
        "gemini_key": bool(os.getenv("GEMINI_API_KEY")),
        "openai_key": bool(os.getenv("OPENAI_API_KEY")),
        "gemini_model": os.getenv("GEMINI_IMAGE_MODEL","gemini-3.1-flash-image"),
        "openai_model": os.getenv("OPENAI_IMAGE_MODEL","gpt-image-1")
    }


@app.get("/history")
def history(limit: int = 40):
    items = []
    files = sorted(
        [p for p in OUT.iterdir() if p.is_file() and p.suffix.lower() in (".png",".jpg",".jpeg",".webp") and p.name.startswith("render_")],
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )[:max(1, min(limit, 100))]

    for p in files:
        meta_path = p.with_suffix(p.suffix + ".json")
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}

        provider = meta.get("provider")
        if not provider:
            provider = "openai" if "openai" in p.name.lower() else "gemini" if "gemini" in p.name.lower() else "ai"

        items.append({
            "name": p.name,
            "url": f"/storage/{p.name}",
            "path": str(p),
            "provider": provider,
            "created_at": meta.get("created_at", int(p.stat().st_mtime)),
            "preset": meta.get("preset", ""),
            "prompt": meta.get("prompt", ""),
            "style_strength": meta.get("style_strength"),
            "structure_preserve": meta.get("structure_preserve"),
            "structure_strength": meta.get("structure_strength", "strict"),
            "quality": meta.get("quality", ""),
            "aspect_ratio": meta.get("aspect_ratio", ""),
            "use_reference_style": meta.get("use_reference_style", False),
            "reference_name": meta.get("reference_name", "")
        })
    return {"items": items}

@app.delete("/history/{name}")
def delete_history(name: str):
    safe = Path(name).name
    if safe != name or not safe.startswith("render_"):
        raise HTTPException(status_code=400, detail="Invalid history filename.")
    target = OUT / safe
    if not target.exists():
        raise HTTPException(status_code=404, detail="History image not found.")
    target.unlink()
    meta = target.with_suffix(target.suffix + ".json")
    if meta.exists():
        meta.unlink()
    return {"ok": True, "name": safe}

@app.post("/render")
async def render(
    provider: str = Form("gemini"),
    image: UploadFile = File(...),
    references: list[UploadFile] = File(default=[]),
    prompt: str = Form(""),
    preset: str = Form("cream"),
    structure_strength: str = Form("strict"),
    aspect_ratio: str = Form("16:9"),
    quality: str = Form("2K"),
    render_quality: str = Form("photo"),
    style_strength: int = Form(70),
    structure_preserve: int = Form(100),
    use_reference_style: str = Form("true"),
    trial_token: str = Form("")):

    charged_user_id = None
    charged_job_id = None
    remaining_points = None

    try:
        # Server-side gate: no valid trial/member token = no AI call.
        charged_user_id, charged_job_id, remaining_points = reserve_render_points(trial_token, provider)

        main = await image.read()
        if not main:
            raise RuntimeError("SketchUp 原圖為空。")

        refs = []
        for f in references[:5]:
            raw = await f.read()
            if raw:
                refs.append((raw, f.content_type or "image/jpeg"))

        style_on = str(use_reference_style).lower() == "true"
        style_pct = max(0, min(100, int(style_strength)))
        preserve_pct = max(60, min(100, int(structure_preserve)))

        ref_instruction = ""
        if refs and style_on:
            custom_reference_mode = preset == "custom"
            ref_instruction = f"""STYLE REFERENCE PRIORITY: {style_pct}%.
CUSTOM REFERENCE MODE: {"ON" if custom_reference_mode else "OFF"}.
The SECOND image is STYLE-ONLY.
Transfer ONLY:
- material palette
- wood tone and grain character
- stone / plaster / paint appearance
- color temperature
- lighting softness and indirect-light language
- decoration density
- overall atmosphere and visual finish

NEVER TRANSFER FROM THE REFERENCE:
- room layout
- camera angle or lens
- wall positions
- doors or windows
- ceiling geometry
- cabinet geometry or cabinet positions
- furniture positions
- appliances
- circulation layout

When style and structure conflict, the FIRST SketchUp image wins."""

        geometry_instruction = f"""SKETCHUP GEOMETRY LOCK PRIORITY: {preserve_pct}%.
The FIRST image (SketchUp source) is the MASTER GEOMETRY and MASTER CAMERA.
Its visible geometry must be treated as fixed design information, not as inspiration.

MANDATORY PRESERVATION RULES:
- Keep the exact camera viewpoint, perspective, crop and field of view.
- Do not move, resize, add or remove walls.
- Do not add, remove or relocate doors, windows or openings.
- Do not change ceiling drops, beams, soffits, niches or level changes.
- Do not move, add or remove built-in cabinets, tall cabinets, TV wall, shelves or fixed joinery.
- Do not move, add or remove major furniture footprints.
- Do not invent washing machines, appliances, rooms, corridors, furniture or architectural elements that are not visible in the SketchUp source.
- Preserve the left/right/front/back spatial relationships of all visible objects.
- Preserve circulation paths and room proportions.

ALLOWED CHANGES ONLY:
materials, surface finishes, wood species/tone, stone/plaster appearance, lighting quality, color palette, soft furnishings, decor and photorealistic detailing.

If the style reference conflicts with the SketchUp geometry, ALWAYS follow the SketchUp geometry."""

        full_prompt = "\n\n".join(filter(None, [
            STRUCTURE.get(structure_strength, STRUCTURE["strict"]),
            geometry_instruction,
            ref_instruction,
            PRESETS.get(preset,""),
            PHOTO_QUALITY.get(render_quality, PHOTO_QUALITY["photo"]),
            ("Gemini image rendering: prioritize physically plausible lighting, material micro-detail and photographic realism while obeying geometry lock." if provider == "gemini" else "OpenAI image rendering: prioritize true photographic material response, realistic exposure, texture fidelity and architecture-photo realism while obeying geometry lock."),
            prompt.strip(),
            "Create a highly photorealistic professional interior architectural render. No labels, no watermark, no explanatory text."
        ]))

        if provider == "gemini":
            b64, mime = await gemini.render(main, image.content_type or "image/png", refs, full_prompt, aspect_ratio, quality)
        elif provider == "openai":
            b64, mime = await openai_provider.render(main, image.content_type or "image/png", refs, full_prompt, aspect_ratio, quality)
        else:
            raise RuntimeError("不支援的 AI 引擎。")

        ext = ".png" if "png" in mime else ".jpg"
        out = OUT / f"render_{provider}_{int(time.time())}{ext}"
        out.write_bytes(base64.b64decode(b64))

        meta = {
            "provider": provider,
            "created_at": int(time.time()),
            "preset": preset,
            "prompt": prompt.strip(),
            "style_strength": style_pct,
            "structure_preserve": preserve_pct,
            "structure_strength": structure_strength,
            "quality": quality,
            "render_quality": render_quality,
            "aspect_ratio": aspect_ratio,
            "use_reference_style": style_on
        }
        meta_path = out.with_suffix(out.suffix + ".json")
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        complete_render_job(charged_user_id, charged_job_id, str(out))

        return {
            "message": f"{provider.upper()} 渲染完成｜本次扣除 {RENDER_COST} 點｜剩餘 {remaining_points} 點",
            "image_data_url": f"data:{mime};base64,{b64}",
            "saved_to": str(out),
            "remaining_points": remaining_points,
            "render_cost": RENDER_COST,
            "job_id": charged_job_id
        }

    except HTTPException:
        # Login / insufficient points are not AI failures and must not be refunded.
        raise
    except Exception as e:
        if charged_user_id is not None and charged_job_id:
            refund_render_points(charged_user_id, charged_job_id, f"AI 生成失敗：{str(e)}")
        raise HTTPException(status_code=500, detail=f"AI 生成失敗，已自動退回 {RENDER_COST} 點。原因：{str(e)}")


# Website portal integrated with v4.5
PORTAL_DIR = Path(__file__).parent / "portal"
if PORTAL_DIR.exists():
    app.mount("/portal", StaticFiles(directory=str(PORTAL_DIR), html=True), name="portal")
