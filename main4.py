import os
import time
import base64
import zipfile
import ast
import numpy as np
import cv2
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# FIXED for duck.ai redesign (Oct 2026):
#   - Tools -> "Create Image" (old "Image" tab is gone)
#   - "Continue" consent gate auto-clicked
#   - robust prompt parsing (survives "my_prompts = [...]" pastes, stray ')')
# BACKGROUND REMOVAL upgraded: floodFill replaced by rembg AI models.
#   BG_MODELS env picks the model(s), comma separated, or "all":
#     birefnet-general      best quality (2026 SOTA edges)  ~1GB, slower
#     birefnet-general-lite lighter/faster BiRefNet         ~440MB
#     isnet-general-use     fast, very good                 ~174MB
#     u2net                 legacy baseline                 ~176MB
#     silueta               tiny legacy baseline            ~40MB
#   All are MIT/Apache licensed -> safe to sell on Redbubble.
#   One model  -> zip layout same as before (no_bg_1.png)
#   2+ models  -> each image is cut by EVERY model; inside remove_N.zip
#                 each model gets its own folder (modelname/no_bg_1.png)
#                 so you can compare them side by side.
#   A hard-edge cleanup removes the soft white fringe stickers get.

SAFE_ALL_MODELS = [
    "birefnet-general",
    "birefnet-general-lite",
    "isnet-general-use",
    "u2net",
    "silueta",
]
_sessions = {}


def parse_models(raw: str) -> list:
    raw = (raw or "").strip().lower()
    if not raw:
        return ["birefnet-general"]  # safe default
    if raw == "all":
        return list(SAFE_ALL_MODELS)
    out = []
    for m in raw.replace(";", ",").split(","):
        m = m.strip()
        if not m:
            continue
        if m == "all":
            out.extend(x for x in SAFE_ALL_MODELS if x not in out)
        elif m not in out:
            out.append(m)
    return out or ["birefnet-general"]


MODELS = parse_models(os.getenv("BG_MODELS") or os.getenv("BG_MODEL"))


def get_session(model: str):
    """Load a rembg session once; returns None if the model fails."""
    if model not in _sessions:
        try:
            from rembg import new_session
            print(f"loading model {model} …")
            _sessions[model] = new_session(model)
        except Exception as e:
            print(f"⚠ model {model} unavailable ({str(e)[:80]}) — skipping it")
            _sessions[model] = None
    return _sessions[model]


def remove_background(raw_bytes: bytes, out_path: str, session) -> bool:
    """AI cutout + hard sticker edge, output PNG with transparency."""
    from rembg import remove as _remove
    cut = _remove(raw_bytes, session=session)
    arr = np.frombuffer(cut, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if img.ndim == 3 and img.shape[2] == 4:
        a = img[:, :, 3]
        a = np.where(a >= 200, 255, np.where(a <= 100, 0, a)).astype(np.uint8)
        img[:, :, 3] = a
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cv2.imwrite(out_path, img)
    return True


def setup_driver():
    return Driver(uc=True, headless=True, no_sandbox=True)


def generate_images(prompts, batch_num):
    driver = setup_driver()
    base_url = "https://duck.ai/chat?duckai=1"

    base_path = os.getcwd()
    images_repo_dir = os.path.join(base_path, "images")
    remove_repo_dir = os.path.join(base_path, "remove")
    temp_raw = os.path.join(base_path, "temp_raw")
    temp_no_bg = os.path.join(base_path, "temp_no_bg")

    for d in [images_repo_dir, remove_repo_dir, temp_raw, temp_no_bg]:
        os.makedirs(d, exist_ok=True)

    raw_files = []
    remove_files = []

    try:
        for i, prompt in enumerate(prompts):
            print(f"--- Processing {i+1}/{len(prompts)} ---")
            driver.get(base_url)
            time.sleep(6)

            try:
                try:
                    WebDriverWait(driver, 4).until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//button[contains(text(), 'Agree')]"))
                    ).click()
                except Exception:
                    pass

                WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(., 'Tools')]"))).click()
                time.sleep(1)
                WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable(
                        (By.XPATH, "//button[contains(., 'Create Image')]"))).click()
                time.sleep(1)

                textarea = WebDriverWait(driver, 15).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, "textarea")))
                textarea.send_keys(prompt + Keys.ENTER)
                time.sleep(3)

                try:
                    driver.find_element(
                        By.XPATH, "//button[normalize-space()='Continue']"
                    ).click()
                    time.sleep(1)
                except Exception:
                    pass

                start_time = time.time()
                captured = False
                while time.time() - start_time < 180:
                    imgs = driver.find_elements(
                        By.XPATH, "//img[contains(@src, 'data:image')]")
                    if imgs:
                        b64data = imgs[-1].get_attribute("src").split(",", 1)[1]
                        raw_bytes = base64.b64decode(b64data)
                        raw_path = os.path.join(temp_raw, f"raw_{i+1}.jpg")
                        with open(raw_path, "wb") as f:
                            f.write(raw_bytes)

                        for model in MODELS:
                            sess = get_session(model)
                            if sess is None:
                                continue
                            out_path = os.path.join(
                                temp_no_bg, model, f"no_bg_{i+1}.png")
                            try:
                                remove_background(raw_bytes, out_path, sess)
                                remove_files.append((model, out_path))
                                print(f"  cut with {model}")
                            except Exception as e:
                                print(f"⚠ {model} failed on image {i+1}: "
                                      f"{str(e)[:100]}")

                        raw_files.append(raw_path)
                        print(f"Captured Image {i+1}")
                        captured = True
                        break
                    time.sleep(5)
                if not captured:
                    print(f"Timeout waiting for image {i+1}")
            except Exception as e:
                print(f"Error on prompt {i+1}: {e}")

        raw_files = [f for f in raw_files if os.path.exists(f)]
        remove_files = [(m, f) for m, f in remove_files if os.path.exists(f)]

        if raw_files:
            z_path = os.path.join(images_repo_dir, f"gen_{batch_num}.zip")
            with zipfile.ZipFile(z_path, 'w') as z:
                for f in raw_files: z.write(f, os.path.basename(f))
            print(f"Created: {z_path}")
        else:
            print("No raw images captured for this batch.")

        if remove_files:
            z_path = os.path.join(remove_repo_dir, f"remove_{batch_num}.zip")
            multi = len(MODELS) > 1
            with zipfile.ZipFile(z_path, 'w') as z:
                for model, f in remove_files:
                    if multi:
                        z.write(f, arcname=os.path.join(model, os.path.basename(f)))
                    else:
                        z.write(f, arcname=os.path.basename(f))
            print(f"Created: {z_path} "
                  f"({len(remove_files)} cutouts from {len(MODELS)} models)")

    finally:
        driver.quit()


def parse_prompts(raw: str) -> list:
    """Accepts 'my_prompts = [...]', bare '[...]', stray ')' or commas."""
    raw = raw.strip()
    if "=" in raw:
        head = raw.split("=", 1)[0].strip()
        if head.replace("_", "").isalpha():
            raw = raw.split("=", 1)[1].strip()
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end > start:
        raw = raw[start:end + 1]
    data = ast.literal_eval(raw)
    if not isinstance(data, list):
        raise ValueError("not a list")
    return [str(p).strip() for p in data if str(p).strip()]


if __name__ == "__main__":
    raw_input = os.getenv("USER_PROMPTS", "").strip()
    manual_zip_num = os.getenv("ZIP_NUM", "1")

    if raw_input:
        try:
            my_prompts = parse_prompts(raw_input)
            print(f"Parsed {len(my_prompts)} prompts. Batch number: {manual_zip_num}")
            print(f"Background-removal models ({len(MODELS)}): {', '.join(MODELS)}")
            generate_images(my_prompts, manual_zip_num)
        except Exception as e:
            print(f"Parsing error: {e}")
            print(f"Raw input was: {raw_input[:200]}")
    else:
        print("No prompts provided.")
