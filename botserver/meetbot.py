# botserver/meetbot.py
import os
import sys
import time
import platform
import subprocess
import argparse
import shutil
import atexit
from pathlib import Path
from threading import Thread

from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


# ========= Helpers =========
def remove_singleton_locks(folder: Path):
    """Xóa các file lock nếu còn sót lại (do crash) để tránh 'profile in use'."""
    for name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        p = folder / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


# ========= Bot class =========
class MeetBot:
    def __init__(
        self,
        meet_link: str,
        profile_dir: str = "./profiles",
        profile_name: str = "meetbot",
        headless: bool = False,
        min_members: int = 2,
        min_record_seconds: int = 200,
        bot_name: str = "Recorder Bot",
    ):
        if not meet_link:
            raise ValueError("meet_link is required")

        self.meet_link = meet_link
        self.profile_root = Path(profile_dir).expanduser().resolve() / profile_name
        self.profile_root.mkdir(parents=True, exist_ok=True)
        remove_singleton_locks(self.profile_root)

        self.headless = headless
        self.min_members = int(min_members)
        self.min_record_seconds = int(min_record_seconds)
        self.bot_name = bot_name

        self.browser = None
        self.rec_proc = None

    # ---------- Chrome ----------
    def _build_driver(self):
        opts = webdriver.ChromeOptions()

        # Dùng profile riêng (đã seed login hoặc để guest dùng tên bot)
        opts.add_argument(f"--user-data-dir={str(self.profile_root)}")
        opts.add_argument("--profile-directory=Default")

        # Giảm popups
        opts.add_argument("--use-fake-ui-for-media-stream")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")

        # Giảm dấu vết automation (không vượt cơ chế bảo mật)
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--disable-blink-features=AutomationControlled")

        if self.headless:
            # Tránh headless khi cần ổn định login; nếu đã seed cookie có thể bật.
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1280,800")

        service = Service(ChromeDriverManager().install())
        self.browser = webdriver.Chrome(service=service, options=opts)

        atexit.register(self._quit_driver)

    def _quit_driver(self):
        try:
            if self.browser:
                self.browser.quit()
        except Exception:
            pass
        remove_singleton_locks(self.profile_root)

    # ---------- Recorder ----------
    def _recorder_run(self):
        """
        macOS: dùng avfoundation để ghi màn hình. Nếu cần ghi system audio,
        cài BlackHole và thay 'screen_index:none' -> 'screen_index:audio_index'.
        Xem thiết bị: ffmpeg -f avfoundation -list_devices true -i ""
        """
        ts = time.strftime("%Y%m%d-%H%M%S")
        if platform.system() == "Darwin":
            # Đổi "1:none" theo index màn hình và audio của bạn
            cmd = [
                "ffmpeg",
                "-f", "avfoundation",
                "-r", "25",
                "-i", "1:none",  # ví dụ chỉ ghi hình, không ghi system audio
                "-pix_fmt", "yuv420p",
                "-preset", "ultrafast",
                "-crf", "18",
                "-y", f"./output-{ts}.mp4",
            ]
        else:
            # Linux: X11 + Pulse (điều chỉnh nguồn audio và kích thước nếu cần)
            cmd = (
                "ffmpeg -f pulse -ac 2 -i default "
                "-f x11grab -r 25 -s 1920x1080 -i :0.0 "
                "-vcodec libx264 -pix_fmt yuv420p -preset ultrafast -crf 18 "
                "-acodec aac -b:a 128k -y "
                f"./output-{ts}.mkv"
            ).split()

        self.rec_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def _recorder_stop(self):
        try:
            if self.rec_proc:
                self.rec_proc.terminate()
        except Exception:
            pass

    # ---------- Guest helpers ----------
    def _fill_guest_name_if_needed(self):
        """
        Nếu trang yêu cầu nhập tên (guest flow), điền self.bot_name.
        Thử nhiều locator để chịu đa ngôn ngữ/biến thể UI.
        """
        wait = WebDriverWait(self.browser, 10)
        name_input = None
        candidates = [
            (By.CSS_SELECTOR, 'input[aria-label="Your name"]'),
            (By.CSS_SELECTOR, 'input[aria-label="Tên của bạn"]'),
            (By.XPATH, '//input[@name="name" or @aria-label="Your name" or @aria-label="Tên của bạn"]'),
            (By.XPATH, '//*[@role="textbox"]'),
        ]
        for how, sel in candidates:
            try:
                el = wait.until(EC.presence_of_element_located((how, sel)))
                if el and el.is_displayed():
                    name_input = el
                    break
            except Exception:
                pass

        if name_input:
            try:
                name_input.clear()
            except Exception:
                pass
            name_input.send_keys(self.bot_name)
            time.sleep(0.5)
            return True
        return False

    def _click_ask_to_join(self):
        """
        Bấm nút 'Ask to join' / 'Yêu cầu tham gia' / 'Tham gia' (khi là khách).
        Dùng nhiều cách dò để chịu đổi ngôn ngữ.
        """
        wait = WebDriverWait(self.browser, 10)
        xpaths = [
            # Nút có text
            '//button[.//span[normalize-space(text())="Ask to join"]]',
            '//button[.//span[normalize-space(text())="Yêu cầu tham gia"]]',
            '//button[.//span[normalize-space(text())="Tham gia"]]',
            # Nút chính theo class cũ
            '//*[contains(concat(" ", normalize-space(@class), " "), " snByac ")]',
            # Button role + text chứa 'join'/'tham gia'
            '//*[@role="button" and .//span[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "join") or contains(normalize-space(.), "tham gia")]]',
        ]
        for xp in xpaths:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
                btn.click()
                return True
            except Exception:
                pass

        # Thử JS click "Join now" theo class Desktop cũ
        try:
            self.browser.execute_script('document.getElementsByClassName("snByac")[1]?.click?.()')
            return True
        except Exception:
            return False

    # ---------- Meet flow ----------
    def _meet_join(self):
        self.browser.get(self.meet_link)
        time.sleep(6)

        is_mac = platform.system() == "Darwin"
        META = Keys.COMMAND if is_mac else Keys.CONTROL

        # tắt cam + mic (Cmd/Ctrl + e, d)
        try:
            body = self.browser.find_element(By.TAG_NAME, "body")
            body.send_keys(META + "e")
            body.send_keys(META + "d")
            time.sleep(1.5)
        except Exception:
            pass

        # Guest: điền tên nếu thấy input name
        self._fill_guest_name_if_needed()

        # Bấm Ask to join (guest) hoặc Join now (account đã login)
        self._click_ask_to_join()

        # Chờ trạng thái chuyển tiếp
        time.sleep(5)

    def _meeting_watch(self):
        tic = time.perf_counter()
        while True:
            meetingleft = ""
            # Nếu bị đẩy về màn hình home, reload và join lại
            for classname in ["j7nIZb", "nS35F"]:
                try:
                    elem = self.browser.find_element(
                        By.XPATH,
                        f'//*[contains(concat(" ", normalize-space(@class), " "), " {classname} ")]'
                        f'//*[contains(concat(" ", normalize-space(@class), " "), " snByac ")]'
                    )
                    meetingleft = (elem.text or "").strip()
                    if meetingleft == "Return to home screen":
                        self.browser.execute_script("location.reload();")
                        self._meet_join()
                        break
                except Exception:
                    pass

            # Sau thời gian tối thiểu, kiểm tra số người để quyết định rời
            toc = time.perf_counter()
            if (toc - tic) > self.min_record_seconds:
                try:
                    # ⚠️ XPath phụ thuộc UI Meet, có thể cần cập nhật theo thời gian
                    mem_text = self.browser.find_element(
                        By.XPATH,
                        '//*[@id="ow3"]/div[1]/div/div[4]/div[3]/div[6]/div[3]/div/div[2]/div[1]/span/span/div/div/span[2]'
                    ).text.strip()
                    members = int(mem_text)
                    print(f"[meetbot] Participants: {members}")
                    if members < self.min_members:
                        break
                except Exception:
                    # Không đọc được số người -> bỏ qua lần này
                    pass

            time.sleep(6)

    def run(self):
        self._build_driver()
        self._meet_join()

        t_rec = Thread(target=self._recorder_run, daemon=True)
        t_mon = Thread(target=self._meeting_watch, daemon=True)

        t_rec.start()
        t_mon.start()

        try:
            t_mon.join()
        finally:
            self._recorder_stop()
            self._quit_driver()


# ========= Public API (để Django view gọi) =========
def run_bot(
    meet_link: str,
    profile_dir: str = "./profiles",
    profile_name: str = "meetbot",
    headless: bool = False,
    min_members: int = 2,
    min_record_seconds: int = 200,
    bot_name: str = "Recorder Bot",
):
    bot = MeetBot(
        meet_link=meet_link,
        profile_dir=profile_dir,
        profile_name=profile_name,
        headless=headless,
        min_members=min_members,
        min_record_seconds=min_record_seconds,
        bot_name=bot_name,
    )
    bot.run()


# ========= CLI =========
def _parse_args():
    p = argparse.ArgumentParser(description="Google Meet Bot (macOS-ready)")
    p.add_argument("meetlink", help="Google Meet link, e.g. https://meet.google.com/abc-defg-hij")
    p.add_argument("--profile-dir", default="./profiles", help="Base folder to store bot profiles")
    p.add_argument("--profile-name", default="meetbot", help="Profile name under profile-dir")
    p.add_argument("--headless", action="store_true", help="Run Chrome headless (not recommended for login)")
    p.add_argument("--min-members", type=int, default=2, help="Leave meeting if participants drop below this")
    p.add_argument("--min-record-seconds", type=int, default=200, help="Minimum recording time before checking members")
    p.add_argument("--bot-name", default="Recorder Bot", help="Display name for guest join flow")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    run_bot(
        meet_link=args.meetlink,
        profile_dir=args.profile_dir,
        profile_name=args.profile_name,
        headless=args.headless,
        min_members=args.min_members,
        min_record_seconds=args.min_record_seconds,
        bot_name=args.bot_name,
    )
