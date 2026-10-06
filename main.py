import os
import time
import base64
import zipfile
import cv2
import ast
import numpy as np
from seleniumbase import Driver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

# FIXED for duck.ai redesign (Oct 2026):
#   - the old "Image" tab is gone -> now Tools -> "Create Image"
#   - a "Continue" consent gate appears on first send -> auto-clicked
#   - robust prompt parsing: survives "my_prompts = [...]" pastes,
#     stray trailing ")" and trailing commas (was: unmatched ')' error)
# Background removal (floodFill from corner) unchanged.

# --- BACKGROUND REMOVAL LOGIC ---
def remove_background(image: np.ndarray, start_point: tuple, threshold: list) -> np.ndarray:
    if image.shape[2] == 4:
        img_bgr = cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)
    else:
        img_bgr = image.copy()
    rows, cols = img_bgr.shape[:2]
    mask = np.zeros((rows + 2, cols + 2), np.uint8)
    flags = (8 | cv2.FLOODFILL_MASK_ONLY | cv2.FLOODFILL_FIXED_RANGE)
    lo_diff = tuple(threshold)
    up_diff = tuple(threshold)
    cv2.floodFill(img_bgr, mask, start_point, newVal=(0, 0, 0), loDiff=lo_diff, upDiff=up_diff, flags=flags)
    result = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2BGRA)
    mask_cut = mask[1:-1, 1:-1]
    result[:, :, 3] = np.where(mask_cut == 1, 0, 255).astype(np.uint8)
    kernel = np.ones((3, 3), np.uint8)
    result[:, :, 3] = cv2.morphologyEx(result[:, :, 3], cv2.MORPH_OPEN, kernel)
    transparent = result[:, :, 3] == 0
    result[transparent, :3] = 0
    return result

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
                # optional legacy gate (some regions may still show it)
                try:
                    WebDriverWait(driver, 4).until(
                        EC.element_to_be_clickable(
                            (By.XPATH, "//button[contains(text(), 'Agree')]"))
                    ).click()
                except Exception:
                    pass

                # new UI: open Tools, pick "Create Image"
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

                # new consent gate on first send of a session
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
                        raw_path = os.path.join(temp_raw, f"raw_{i+1}.jpg")
                        with open(raw_path, "wb") as f:
                            f.write(base64.b64decode(b64data))

                        img_cv = cv2.imread(raw_path)
                        if img_cv is not None:
                            no_bg = remove_background(img_cv, (2, 2), [200, 200, 200])
                            no_bg_path = os.path.join(temp_no_bg, f"no_bg_{i+1}.png")
                            cv2.imwrite(no_bg_path, no_bg)

                            raw_files.append(raw_path)
                            remove_files.append(no_bg_path)
                            print(f"Captured Image {i+1}")
                            captured = True
                        break
                    time.sleep(5)
                if not captured:
                    print(f"Timeout waiting for image {i+1}")
            except Exception as e:
                print(f"Error on prompt {i+1}: {e}")

        # Final check and Zipping
        raw_files = [f for f in raw_files if os.path.exists(f)]
        remove_files = [f for f in remove_files if os.path.exists(f)]

        if raw_files:
            z_path = os.path.join(images_repo_dir, f"gen_{batch_num}.zip")
            with zipfile.ZipFile(z_path, 'w') as z:
                for f in raw_files: z.write(f, os.path.basename(f))
            print(f"Created: {z_path}")
        else:
            print("No raw images captured for this batch.")

        if remove_files:
            z_path = os.path.join(remove_repo_dir, f"remove_{batch_num}.zip")
            with zipfile.ZipFile(z_path, 'w') as z:
                for f in remove_files: z.write(f, os.path.basename(f))
            print(f"Created: {z_path}")

    finally:
        driver.quit()


def parse_prompts(raw: str) -> list:
    """Robust parse: accepts 'my_prompts = [...]', bare '[...]',
    stray trailing ')' or trailing commas from pasting."""
    raw = raw.strip()
    if "=" in raw:
        head = raw.split("=", 1)[0].strip()
        if head.replace("_", "").isalpha():  # leading assignment like my_prompts =
            raw = raw.split("=", 1)[1].strip()
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end > start:
        raw = raw[start:end + 1]  # keep only the bracketed list
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
            generate_images(my_prompts, manual_zip_num)
        except Exception as e:
            print(f"Parsing error: {e}")
            print(f"Raw input was: {raw_input[:200]}")
    else:
        print("No prompts provided.")
