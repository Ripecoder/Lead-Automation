from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import time
import os
import json
import subprocess
import shutil

def get_profile_display_name(profile_path):
    """Get the display name from Chrome profile"""
    try:
        prefs_file = os.path.join(profile_path, "Preferences")
        if os.path.exists(prefs_file):
            with open(prefs_file, 'r', encoding='utf-8', errors='ignore') as f:
                prefs = json.load(f)
                if 'profile' in prefs and 'name' in prefs['profile']:
                    return prefs['profile']['name']
    except Exception as e:
        print(f"   (couldn't read display name: {e})")
    return "Unknown"

def list_all_profiles():
    """Show all Chrome profiles"""
    user_data_dir = r"C:\Users\SMART\AppData\Local\Google\Chrome\User Data"

    profiles = {}

    if not os.path.exists(user_data_dir):
        print("❌ Chrome User Data folder not found!")
        return profiles

    print("\n📱 Available Chrome Profiles:\n")

    count = 1
    for profile_name in sorted(os.listdir(user_data_dir)):
        profile_path = os.path.join(user_data_dir, profile_name)

        if not os.path.isdir(profile_path):
            continue
        # Only real profile folders look like this
        if profile_name not in ("Default",) and not profile_name.startswith("Profile "):
            continue

        display_name = get_profile_display_name(profile_path)
        profiles[count] = (profile_name, display_name)

        print(f"   {count}. {display_name} → (Folder: {profile_name})")
        count += 1

    return profiles

def make_temp_profile_copy(user_data_dir, profile_folder_name, force_refresh=False):
    """
    Use a persistent automation profile folder so you only need to scan
    the WhatsApp QR code ONCE. On the very first run (or if force_refresh
    is True), it seeds this folder from your real Chrome profile. On every
    run after that, it just reuses the same persistent folder, which is
    never touched by your everyday Chrome/Windows session, so no
    SingletonLock / DevToolsActivePort conflicts.
    """
    # Note: fixed, persistent location (NOT the OS temp dir, which some
    # systems clear on reboot). Change this path if you'd like it elsewhere.
    automation_root = r"C:\ChromeAutomationProfiles\whatsapp_selenium_profile"
    dst_profile = os.path.join(automation_root, profile_folder_name)
    already_seeded = os.path.isdir(dst_profile)

    if already_seeded and not force_refresh:
        print(f"\n📂 Reusing existing automation profile (no re-login needed): {automation_root}\n")
        return automation_root

    print(f"\n📂 Seeding automation profile from your real Chrome profile (first run only)...")

    if os.path.exists(automation_root):
        shutil.rmtree(automation_root, ignore_errors=True)
    os.makedirs(automation_root, exist_ok=True)

    local_state_src = os.path.join(user_data_dir, "Local State")
    if os.path.exists(local_state_src):
        shutil.copy2(local_state_src, os.path.join(automation_root, "Local State"))

    src_profile = os.path.join(user_data_dir, profile_folder_name)

    try:
        shutil.copytree(
            src_profile,
            dst_profile,
            ignore=shutil.ignore_patterns(
                "Singleton*", "*.lock", "lockfile", "Crashpad", "GPUCache", "Cache", "Code Cache"
            ),
        )
        print(f"✓ Profile seeded at: {automation_root}")
        print("  (You'll need to scan the QR code once now. After this, it will stay logged in.)\n")
    except Exception as e:
        print(f"❌ Could not copy profile: {e}")
        return None

    return automation_root

def open_whatsapp_with_profile(profile_folder_name, force_refresh=False):
    """Open WhatsApp Web with the specified profile (via a persistent automation copy)"""

    print(f"\n🚀 Opening Chrome with profile: {profile_folder_name}...")

    real_user_data_dir = r"C:\Users\SMART\AppData\Local\Google\Chrome\User Data"

    automation_user_data_dir = make_temp_profile_copy(real_user_data_dir, profile_folder_name, force_refresh)
    if not automation_user_data_dir:
        return None

    options = webdriver.ChromeOptions()
    options.add_argument(f"user-data-dir={automation_user_data_dir}")
    options.add_argument(f"profile-directory={profile_folder_name}")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--start-maximized")
    options.add_argument("--no-first-run")
    options.add_argument("--no-default-browser-check")
    options.add_argument("--remote-debugging-port=9222")

    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)

    try:
        driver = webdriver.Chrome(options=options)
        print(f"✓ Chrome opened!\n")

        print(f"Going to WhatsApp Web...")
        driver.get("https://web.whatsapp.com/")

        print("⏳ Waiting for WhatsApp to load (this may take 10-15 seconds)...\n")

        wait = WebDriverWait(driver, 20)

        try:
            print("🔍 Checking if you're logged in...")
            wait.until(EC.presence_of_element_located((By.XPATH, "//div[@role='navigation']")))
            print("✓ Already logged in! Chat list loaded.\n")

        except Exception as e:
            print(f"   (not logged in yet: {e})")
            print("📱 Waiting for QR code / login...")
            print("⚠️  Please scan the QR code with your phone to log in.\n")
            print("Waiting for you to scan... (max 60 seconds)")

            try:
                long_wait = WebDriverWait(driver, 60)
                long_wait.until(EC.invisibility_of_element_located((By.XPATH, "//canvas")))
                print("✓ Scan successful! WhatsApp loading...\n")
                time.sleep(3)

            except Exception as e2:
                print(f"⏱️  Scan timeout or page still loading: {e2}")
                print("✓ WhatsApp may still be loading — check the browser window.\n")

        print(f"URL: {driver.current_url}\n")
        return driver

    except Exception as e:
        print(f"❌ Error: {e}")
        print("\nTry:")
        print("1. Make sure Chrome closed completely")
        print("2. taskkill /F /IM chrome.exe /T (run as admin)")
        print("3. taskkill /F /IM chromedriver.exe /T (run as admin)")
        print("4. Delete any leftover 'SingletonLock' file in the profile folder manually")
        print("5. Restart the script")
        return None

# Kill all Chrome + chromedriver processes first
print("🔴 Closing any open Chrome windows...")
subprocess.run("taskkill /F /IM chrome.exe /T", shell=True, capture_output=True)
subprocess.run("taskkill /F /IM chromedriver.exe /T", shell=True, capture_output=True)
time.sleep(2)

# Show all profiles
profiles = list_all_profiles()

if profiles:
    print("\n" + "=" * 50)
    choice = input("Pick a profile number: ").strip()

    if choice.isdigit() and int(choice) in profiles:
        profile_folder, display_name = profiles[int(choice)]
        print(f"\n✓ Selected: {display_name}")
    else:
        profile_folder = choice
        print(f"\n✓ Using profile folder: {profile_folder}")

    force_refresh = "--refresh-profile" in os.sys.argv
    driver = open_whatsapp_with_profile(profile_folder, force_refresh=force_refresh)

    if driver:
        print("=" * 50)
        print(f"✓ WhatsApp Web is ready!")
        print("=" * 50)

        print("\nPress Ctrl+C to close.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n\nClosing browser...")
            driver.quit()
            print("Done!")
    else:
        print("Failed to open WhatsApp")
else:
    print("No profiles found!")