# botserver/meetbot.py
import os
import time
import platform
import subprocess
import argparse
import shutil
import atexit
import tempfile
import json
import urllib.request, urllib.error
from pathlib import Path
from threading import Thread

from selenium import webdriver
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


def remove_singleton_locks(folder: Path):
    for name in ["SingletonLock", "SingletonCookie", "SingletonSocket"]:
        p = folder / name
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


class MeetBot:
    def __init__(
        self,
        meet_link: str,
        profile_dir: str = "./profiles",
        profile_name: str = "meetbot",
        headless: bool = False,
        min_members: int = 1,
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
        self.rec_output_path = None
        
        self.webhook_url = os.getenv("WEBHOOK_URL", "").strip() or None
        self.public_base = os.getenv("REC_PUBLIC_BASE", "").rstrip("/")
        self.message_id = os.getenv("MESSAGE_ID", "").strip() or None

        # profile tạm cho mỗi lần chạy
        self._tmp_profile = Path(tempfile.mkdtemp(prefix="meetbot_", dir="/tmp")).resolve()

    # ---------- Chrome ----------
    def _build_driver(self):
        print('Building Chrome driver...')
        W = os.getenv("REC_WIDTH", "1366")
        H = os.getenv("REC_HEIGHT", "768")
        opts = webdriver.ChromeOptions()
        opts.add_argument(f"--user-data-dir={str(self._tmp_profile)}")
        opts.add_argument("--profile-directory=Default")
        opts.add_argument("--use-fake-ui-for-media-stream")
        opts.add_argument("--disable-notifications")
        opts.add_argument("--no-first-run")
        opts.add_argument("--no-default-browser-check")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_argument("--no-sandbox")
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument(f"--window-size={W},{H}")
        opts.add_argument("--window-position=0,0")
        opts.add_argument("--force-device-scale-factor=1")
        opts.add_argument("--high-dpi-support=1")
        if self.headless:
            opts.add_argument("--headless=new")
            opts.add_argument("--window-size=1920,1080")

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
        try:
            if self._tmp_profile and self._tmp_profile.exists():
                shutil.rmtree(self._tmp_profile, ignore_errors=True)
        except Exception:
            pass

    # ---------- Recorder (FULLSCREEN + HIGH QUALITY) ----------
    def _recorder_run(self):
        """
        Bắt đầu ffmpeg ghi màn hình sau khi đã join thành công.

        Tuỳ biến bằng env:
          - REC_FPS (mặc định 15)
          - REC_WIDTH, REC_HEIGHT (mặc định 1920x1080; khớp Xvfb)
          - REC_LOSSLESS=1|0 (mặc định 1: CRF 0 lossless; 0: CRF 14 rất nét)
          - REC_DIR (Linux: mặc định /var/app/recordings; macOS: ./recordings)
        """
        print("[meetbot] Starting screen recorder (fullscreen, high quality)...")
        ts = time.strftime("%Y%m%d-%H%M%S")
        fps = int(os.getenv("REC_FPS", "15"))
        lossless = os.getenv("REC_LOSSLESS", "0").lower() in ("1","true","yes")

        # Linux/Docker: dùng Xvfb DISPLAY
        disp = os.environ.get("DISPLAY", ":99")
        out_dir = Path(os.getenv("REC_DIR", "/var/app/recordings"))
        out_dir.mkdir(parents=True, exist_ok=True)
        rec_out_env = os.getenv("REC_OUT", "").strip()
        if rec_out_env:
            out_path = str(out_dir / rec_out_env)  # chỉ là "tên file", không path tuyệt đối
        else:
            out_path = str(out_dir / f"output-{ts}.mkv")

        width = os.getenv("REC_WIDTH", "1366")
        height = os.getenv("REC_HEIGHT", "768")  
        # Xvfb phải chạy đúng kích thước này trong entrypoint.sh
        # Xvfb :99 -screen 0 {width}x{height}x24

        v_args = ["-crf", "26"] if not lossless else ["-crf", "0"]
        preset = ["-preset", "medium"]

        cmd = [
            "ffmpeg","-y",
            "-f","pulse","-ac","1","-i","default",        # -ac 1 : mono
            "-f","x11grab","-framerate",str(fps),"-video_size",f"{width}x{height}","-i",disp,
            "-c:v","libx265", *v_args, "-preset","medium", "-pix_fmt","yuv420p",
            "-c:a","aac","-b:a","64k","-ac","1","-ar","48000",  # 192k stereo -> 64k mono
            out_path
        ]

        self.rec_output_path = out_path
        self.rec_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)

    def _recorder_stop(self):
        try:
            if self.rec_proc and self.rec_proc.poll() is None:
                print("[meetbot] Stopping screen recorder...")
                self.rec_proc.terminate()
                try:
                    self.rec_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.rec_proc.kill()
        except Exception:
            pass

    # ---------- UI helpers ----------
    def _fill_guest_name_if_needed(self):
        wait = WebDriverWait(self.browser, 10)
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
                    try:
                        el.clear()
                    except Exception:
                        pass
                    el.send_keys(self.bot_name)
                    time.sleep(0.4)
                    return True
            except Exception:
                pass
        return False

    def _click_ask_to_join(self):
        wait = WebDriverWait(self.browser, 10)
        xpaths = [
            '//button[.//span[normalize-space(text())="Ask to join"]]',
            '//button[.//span[normalize-space(text())="Yêu cầu tham gia"]]',
            '//button[.//span[normalize-space(text())="Tham gia"]]',
            '//*[contains(concat(" ", normalize-space(@class), " "), " snByac ")]',
            '//*[@role="button" and .//span[contains(translate(normalize-space(.),"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"), "join") or contains(normalize-space(.), "tham gia")]]',
        ]
        for xp in xpaths:
            try:
                btn = wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
                btn.click()
                return True
            except Exception:
                pass
        try:
            self.browser.execute_script('document.getElementsByClassName("snByac")[1]?.click?.()')
            return True
        except Exception:
            return False

    def _is_in_call(self) -> bool:
        wait = WebDriverWait(self.browser, 2)
        leave_selectors = [
            (By.CSS_SELECTOR, 'button[aria-label="Leave call"]'),
            (By.CSS_SELECTOR, 'div[aria-label="Leave call"]'),
            (By.XPATH, '//*[@aria-label="Leave call"]'),
            (By.XPATH, '//*[@aria-label="Rời cuộc gọi"]'),
            (By.XPATH, '//*[@aria-label="Kết thúc cuộc gọi"]'),
            (By.XPATH, '//*[@data-tooltip="Leave call" or @data-tooltip="Rời cuộc gọi" or @data-tooltip="Kết thúc cuộc gọi"]'),
        ]
        for how, sel in leave_selectors:
            try:
                el = wait.until(EC.presence_of_element_located((how, sel)))
                if el and el.is_displayed():
                    return True
            except Exception:
                pass

        lobby_signals = [
            '//*[contains(., "Ask to join") or contains(., "Yêu cầu tham gia")]',
            '//*[contains(., "Return to home screen")]',
            '//*[contains(., "You’ve been removed") or contains(., "Bạn đã bị xóa khỏi cuộc họp")]',
            '//*[contains(., "has ended") or contains(., "đã kết thúc") or contains(., "cuộc họp đã kết thúc")]',
            '//*[contains(., "Ready to join") or contains(., "Sẵn sàng tham gia")]',
        ]
        for xp in lobby_signals:
            try:
                el = self.browser.find_element(By.XPATH, xp)
                if el and el.is_displayed():
                    return False
            except Exception:
                pass
        return False

    def _wait_until_joined(self, timeout=600):
        print(f"[meetbot] Waiting to be admitted (≤ {timeout}s)...")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._is_in_call():
                print("[meetbot] Admitted. Join confirmed.")
                return True
            time.sleep(2)
        print("[meetbot] Waited too long but not admitted. Stop.")
        return False

    def _dismiss_popups(self):
        """Tự động bấm các nút 'Got it' / 'Đã hiểu' nếu xuất hiện."""
        try:
            wait = WebDriverWait(self.browser, 2)
            buttons = [
                '//button[.//span[normalize-space(text())="Got it"]]',
                '//button[.//span[normalize-space(text())="Đã hiểu"]]',
                '//*[normalize-space(text())="Got it"]',
                '//*[normalize-space(text())="Đã hiểu"]'
            ]
            for xp in buttons:
                try:
                    btn = wait.until(EC.element_to_be_clickable((By.XPATH, xp)))
                    if btn and btn.is_displayed():
                        btn.click()
                        print("[meetbot] Dismissed popup (Got it).")
                        time.sleep(0.3)
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        return False
    
    def _notify_webhook(self, event: str):
        if not self.webhook_url:
            return
        try:
            fname = Path(self.rec_output_path).name if self.rec_output_path else None
            payload = {
                "event": event,                       # 'record_stopped'
                "filename": fname,                    # ví dụ: rec-xxxx.mkv
                "full_path": self.rec_output_path,    # đường dẫn trên server
                "meet_link": self.meet_link,
                "timestamp": int(time.time()),
                "message_id": self.message_id
            }
            if self.public_base and fname:
                payload["file_url"] = f"{self.public_base}/{fname}"   # vd: http://.../api/recordings/rec-xxxx.mkv"

            req = urllib.request.Request(
                self.webhook_url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            urllib.request.urlopen(req, timeout=5).read()  # không chặn lâu
            print(f"[meetbot] Webhook sent: {self.webhook_url}")
        except Exception as e:
            print(f"[meetbot] Webhook error: {e}")


    # ---------- Meet flow ----------
    def _meet_join(self):
        self.browser.get(self.meet_link)
        try:
            w = int(os.getenv("REC_WIDTH", "1920"))
            h = int(os.getenv("REC_HEIGHT", "1080"))
            self.browser.set_window_position(0, 0)
            self.browser.set_window_size(w, h)
        except Exception:
            pass
        time.sleep(6)

        is_mac = platform.system() == "Darwin"
        META = Keys.COMMAND if is_mac else Keys.CONTROL
        try:
            body = self.browser.find_element(By.TAG_NAME, "body")
            body.send_keys(META + "e")
            body.send_keys(META + "d")
            time.sleep(1.0)
        except Exception:
            pass

        self._fill_guest_name_if_needed()
        self._click_ask_to_join()
        time.sleep(2)

    def _meeting_watch(self, joined_at: float):
        while True:
            self._dismiss_popups()
            if not self._is_in_call():
                print("[meetbot] Not in call anymore (kicked/ended/disconnected).")
                break
            if (time.time() - joined_at) > self.min_record_seconds:
                try:
                    mem_text = self.browser.find_element(
                        By.XPATH,
                        '//*[@id="ow3"]/div[1]/div/div[4]/div[3]/div[6]/div[3]/div/div[2]/div[1]/span/span/div/div/span[2]'
                    ).text.strip()
                    members = int(mem_text)
                    print(f"[meetbot] Participants: {members}")
                    if members < self.min_members:
                        print("[meetbot] Below threshold. Stopping...")
                        break
                except Exception:
                    pass
            time.sleep(4)

    def run(self):
        self._build_driver()
        self._meet_join()

        if not self._wait_until_joined(timeout=600):
            self._quit_driver()
            return

        joined_at = time.time()
        t_rec = Thread(target=self._recorder_run, daemon=True)
        t_mon = Thread(target=self._meeting_watch, args=(joined_at,), daemon=True)
        t_rec.start()
        t_mon.start()
        try:
            t_mon.join()
        finally:
            self._recorder_stop()
            self._notify_webhook(event="record_stopped")
            self._quit_driver()


def run_bot(
    meet_link: str,
    profile_dir: str = "./profiles",
    profile_name: str = "meetbot",
    headless: bool = False,
    min_members: int = 1,
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


def _parse_args():
    p = argparse.ArgumentParser(description="Google Meet Bot (record AFTER admit; fullscreen, high-quality)")
    p.add_argument("meetlink", help="Google Meet link, e.g. https://meet.google.com/abc-defg-hij")
    p.add_argument("--profile-dir", default="./profiles")
    p.add_argument("--profile-name", default="meetbot")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--min-members", type=int, default=1)
    p.add_argument("--min-record-seconds", type=int, default=200)
    p.add_argument("--bot-name", default="Recorder Bot")
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
