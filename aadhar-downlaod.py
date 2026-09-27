import sys
import os
import re
import base64
import requests
import uuid
import ddddocr
from io import BytesIO

# Configuration
BASE_URL = "https://tathya.uidai.gov.in"
CAPTCHA_URL = f"{BASE_URL}/audioCaptchaService/api/captcha/v3/generation"
OTP_URL = f"{BASE_URL}/unifiedAppAuthService/api/v2/generate/aadhaar/otp"
DOWNLOAD_URL = f"{BASE_URL}/downloadAadhaarService/api/aadhaar/download"

_ocr_solver = ddddocr.DdddOcr(show_ad=False)
_ocr_solver_beta = ddddocr.DdddOcr(beta=True, show_ad=False)

def solve_captcha(image_bytes):
    """Advanced captcha solver with PIL preprocessing for UIDAI images."""
    try:
        import io
        from PIL import Image, ImageFilter, ImageEnhance, ImageOps
        
        for attempt in range(2):
            try:
                img = Image.open(io.BytesIO(image_bytes))
                if img.mode != 'L':
                    img = img.convert('L')
                
                w, h = img.size
                img = img.resize((w * 2, h * 2), Image.LANCZOS)
                img = img.filter(ImageFilter.MedianFilter(size=3))
                img = ImageEnhance.Contrast(img).enhance(2.0)
                img = ImageEnhance.Sharpness(img).enhance(2.0)
                img = img.point(lambda p: 255 if p > 140 else 0)
                img = ImageOps.autocontrast(img, cutoff=5)
                
                buf = io.BytesIO()
                img.save(buf, format='PNG', optimize=True)
                processed = buf.getvalue()
                
                result = _ocr_solver_beta.classification(processed)
                if result and len(result) >= 4:
                    result = ''.join(c for c in result if c.isalnum())
                    if len(result) >= 4:
                        return result[:6]
            except Exception:
                pass
    except Exception:
        pass
    
    # Fallback to standard OCR
    res = _ocr_solver.classification(image_bytes)
    return str(res or '').strip()

def run_download(eid, chat_id):
    from uidai_http import make_uidai_session
    session = make_uidai_session(retries=0)
    
    request_id = str(uuid.uuid4())

    # Browser-like strict headers matching Phase 1
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en_IN",
        "Content-Type": "application/json",
        "appid": "MYAADHAAR",
        "x-request-id": request_id,
        "Origin": "https://myaadhaar.uidai.gov.in",
        "Referer": "https://myaadhaar.uidai.gov.in/",
        "User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/149.0.0.0 Mobile Safari/537.36",
        "sec-ch-ua": '"Google Chrome";v="149", "Chromium";v="149", "Not)A;Brand";v="24"',
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "Connection": "keep-alive"
    }

    print("--- STEP 1: Fetching Download Captcha ---")
    captcha_payload = {
        "captchaLength": "6", 
        "captchaType": "2", 
        "audioCaptchaRequired": True
    }
    
    max_captcha_retries = 5
    cap_txn_id = None
    captcha_val = None
    otp_txn_id = None
    last_server_msg = "Failed to send OTP after maximum retries. Please verify details and try again."
    attempt = 1
    captcha_attempts = 0
    max_captcha_attempts = 15

    while attempt <= max_captcha_retries and captcha_attempts < max_captcha_attempts:
        captcha_attempts += 1
        try:
            res_cap = session.post(CAPTCHA_URL, json=captcha_payload, headers=headers, timeout=30)
            res_cap.raise_for_status()
            try:
                cap_data = res_cap.json()
            except ValueError:
                raise Exception("Aadhaar Portal returned an invalid non-JSON page during captcha load. Gateway might be down.")
            
            if not cap_data or 'imageBase64' not in cap_data or 'transactionId' not in cap_data:
                raise Exception("UIDAI captcha generation failed. Invalid server response.")
            
            img_b64 = cap_data['imageBase64']
            cap_txn_id = cap_data['transactionId']

            img_bytes = base64.b64decode(img_b64)
            if attempt >= 3:
                print(f"🔑 MANUAL CAPTCHA REQUIRED | {img_b64}")
                sys.stdout.flush()
                captcha_val = sys.stdin.readline().strip()
                if not captcha_val:
                    raise Exception("No manual captcha entered.")
            else:
                captcha_val = solve_captcha(img_bytes)
                captcha_val = re.sub(r'[^a-zA-Z0-9]', '', captcha_val)
                
                if len(captcha_val) != 6:
                    print(f"⚠️ [OCR] Rejected noisy read '{captcha_val}' (Length {len(captcha_val)} != 6). Fetching new captcha...")
                    continue
                
            print(f"Decoded Captcha: {captcha_val}")
            
            # Request Download OTP
            otp_payload = {
                "eidNumber": eid,
                "idType": "eid",
                "captchaTxnId": cap_txn_id,
                "captchaValue": captcha_val,
                "resendOTP": False,
                "transactionId": request_id
            }

            res_otp = session.post(OTP_URL, json=otp_payload, headers=headers, timeout=45)
            try:
                otp_data = res_otp.json()
            except ValueError:
                raise Exception("Aadhaar Portal returned an invalid non-JSON page during OTP request. Gateway might be down.")

            if otp_data.get('status') == "Success":
                print("✅ OTP Sent Successfully!")
                otp_txn_id = otp_data['txnId']
                break
            else:
                msg = otp_data.get('message') or (otp_data.get('responseData') or {}).get('message') or 'OTP generation failed'
                print(f"Server Response Attempt {attempt}: {msg} | Full JSON: {otp_data}")
                if msg:
                    last_server_msg = msg
                if msg and "technical difficulties" in msg.lower():
                    raise Exception(f"⚠️ UIDAI portal is temporarily facing technical difficulties. Please try again later. ||| Real Server Response: {msg}")
                attempt += 1
        except Exception as ex:
            if attempt == max_captcha_retries or captcha_attempts == max_captcha_attempts:
                raise Exception(f"Failed to solve captcha / send OTP after retries: {ex}")

    if not otp_txn_id:
        raise Exception(last_server_msg)

    # Prompt for OTP
    print("\n" + "="*60)
    print("🔑 ENTER THE OTP RECEIVED ON YOUR REGISTERED MOBILE")
    sys.stdout.flush()
    otp_code = sys.stdin.readline().strip()
    print("="*60)

    if not otp_code:
        raise Exception("No OTP entered.")

    download_payload = {
        "eid": eid,
        "mask": False,
        "otp": otp_code,
        "otpTxnId": otp_txn_id
    }

    download_headers = headers.copy()
    download_headers["transactionId"] = request_id

    print("Downloading Aadhaar PDF from UIDAI secure server...")
    res_dl = session.post(DOWNLOAD_URL, json=download_payload, headers=download_headers, timeout=60)
    dl_data = res_dl.json()

    if dl_data.get('status') == "Success":
        pdf_b64 = dl_data['data']['aadhaarPdf']
        pdf_bytes = base64.b64decode(pdf_b64)
        
        script_dir = os.path.dirname(os.path.abspath(__file__))
        cracked_dir = os.path.join(script_dir, "cracked_aadhar")
        os.makedirs(cracked_dir, exist_ok=True)
        file_path = os.path.join(cracked_dir, f"Aadhaar_{chat_id}.pdf")
        
        with open(file_path, "wb") as f:
            f.write(pdf_bytes)
            
        print(f"\n========================================")
        print(f"🎉 SUCCESS! Aadhaar PDF Downloaded Successfully!")
        print(f"📁 Saved as: {file_path}")
        print(f"========================================")
    else:
        error_msg = dl_data.get('statusMessage', 'Download failed. Please check details/OTP.')
        raise Exception(f"Download failed: {error_msg}")

if __name__ == "__main__":
    if len(sys.argv) >= 3:
        EID = sys.argv[1]
        CHAT_ID = sys.argv[2]
    else:
        print("Usage: python aadhar-downlaod.py <EID> <CHAT_ID>")
        sys.exit(1)
        
    try:
        run_download(EID, CHAT_ID)
    except Exception as e:
        print(f"An error occurred: {e}", file=sys.stderr)
        sys.exit(1)
