"""Create a Zhihuishu browser session with interactive login by default.

The output is a reusable credential. The caller owns that output and must remove
it after the download; temporary challenge images and partial state files are
owned and removed by this command, including on interruption.
"""

import argparse
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlsplit

from ..common import fetch, safe_error
from .aicourse import DEFAULT_STATE, sync_playwright

DEFAULT_SERVICE = "https://onlineweb.zhihuishu.com/onlinestuh5"
LOGIN_BASE = "https://login.zhihuishu.com/?origin=zhs&service="


def load_credentials(args):
    """Environment > CLI > an explicitly selected JSON file; manual is allowed."""
    phone = os.environ.get("ZHS_PHONE") or getattr(args, "phone", None)
    password = os.environ.get("ZHS_PWD") or getattr(args, "pwd", None)
    credentials = getattr(args, "credentials", None)
    if credentials and not (phone and password):
        with open(credentials, encoding="utf-8") as stream:
            data = json.load(stream)
        if not isinstance(data, dict):
            raise ValueError("Credentials JSON must be an object")
        phone = phone or data.get("phone")
        password = password or data.get("password")
    if phone is not None and not isinstance(phone, str):
        raise ValueError("Phone must be a string")
    if password is not None and not isinstance(password, str):
        raise ValueError("Password must be a string")
    return phone, password


def login_error(exc, phone=None, password=None):
    """Playwright call logs may echo values passed to fill; remove them too."""
    message = safe_error(exc)
    for value in (password, phone):
        if value:
            message = message.replace(value, "[redacted]")
    return message


def service_kind(hostname):
    """Return the supported login service kind for a host, or None."""
    host = hostname or ""
    if host == "zhihuishu.com" or host.endswith(".zhihuishu.com"):
        return "zhihuishu"
    if host == "chaoxing.com" or host.endswith(".chaoxing.com"):
        return "chaoxing"
    if host == "icourse163.org" or host.endswith(".icourse163.org"):
        return "icourse163"
    return None


def is_logged_in(page, service):
    """Require a redirect to the requested service host, not an arbitrary page."""
    current = urlsplit(page.url)
    expected = urlsplit(service)
    return current.scheme == "https" and current.hostname == expected.hostname


def wait_for_login(page, service, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_logged_in(page, service):
            return True
        page.wait_for_timeout(250)
    return is_logged_in(page, service)


def prepare_login(page, login_url, phone, password):
    page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
    if phone:
        page.get_by_placeholder("请输入手机号或用户名").fill(phone)
    if password:
        page.get_by_placeholder("请输入密码").fill(password)
    # Use semantic controls only. Unknown custom widgets remain for the user.
    agreement = page.get_by_role("checkbox")
    if agreement.count() == 1:
        try:
            agreement.check(timeout=2000)
        except Exception:
            pass
    if phone and password:
        page.get_by_text("登录", exact=True).last.click()


class ChallengeCapture:
    def __init__(self):
        self.body = None

    def on_response(self, response):
        if "dun.163.com" in response.url and "/v3/get" in response.url:
            try:
                self.body = response.text()
            except Exception:
                self.body = None


def find_gap(panel_png, front_png, current_left, image_width):
    """Optional template matcher, imported only by --auto-captcha execution."""
    import cv2
    import numpy as np

    panel = cv2.imdecode(np.fromfile(panel_png, dtype=np.uint8), cv2.IMREAD_COLOR)
    front = cv2.imdecode(np.fromfile(front_png, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    if panel is None or front is None or front.ndim != 3 or front.shape[2] != 4:
        raise ValueError("Invalid challenge image")
    alpha = front[:, :, 3]
    ys, xs = np.where(alpha > 10)
    if not len(xs):
        raise ValueError("Challenge piece is empty")
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    piece = front[y0:y1, x0:x1, :3]
    mask = (alpha[y0:y1, x0:x1] > 10).astype(np.uint8) * 255
    scale = image_width / 480.0  # Observed original challenge canvas width.
    width = max(1, int(round(piece.shape[1] * scale)))
    height = max(1, int(round(piece.shape[0] * scale)))
    if width > panel.shape[1] or height > panel.shape[0]:
        raise ValueError("Challenge dimensions do not match")
    piece = cv2.resize(piece, (width, height), interpolation=cv2.INTER_AREA)
    mask = cv2.resize(mask, (width, height), interpolation=cv2.INTER_NEAREST)
    match = cv2.matchTemplate(
        cv2.cvtColor(panel, cv2.COLOR_BGR2GRAY),
        cv2.cvtColor(piece, cv2.COLOR_BGR2GRAY),
        cv2.TM_CCOEFF_NORMED,
        mask=mask,
    )
    match = np.nan_to_num(match, nan=-1, posinf=-1, neginf=-1)
    lo, hi = (
        max(0, int(current_left - width - 6)),
        min(match.shape[1], int(current_left + width + 6)),
    )
    if hi > lo:
        match[:, lo:hi] = -1
    _, confidence, _, location = cv2.minMaxLoc(match)
    return location[0], confidence


def human_to(page, x, y, target_x, steps=30):
    for step in range(1, steps + 1):
        fraction = step / steps
        eased = 1 - (1 - fraction) ** 2.2
        page.mouse.move(x + (target_x - x) * eased, y + (step % 5 - 2) * 0.4)
        page.wait_for_timeout(16)


def solve_challenge(page, capture, temporary):
    """Attempt the optional legacy slider solver; temporary files stay isolated."""
    for _ in range(40):
        if capture.body:
            break
        page.wait_for_timeout(250)
    if not capture.body:
        return False
    front_match = re.search(r'"front":\["([^\"]+)"', capture.body)
    if not front_match:
        return False
    front_url = front_match.group(1).replace("\\/", "/")
    if urlsplit(front_url).scheme != "https":
        raise ValueError("Challenge image URL must use HTTPS")
    front_path = os.path.join(temporary, "front.png")
    content = fetch(front_url, timeout=15, max_bytes=8 * 1024 * 1024)
    Path(front_path).write_bytes(content)
    slider = page.locator(".yidun_slider").bounding_box()
    background = page.locator(".yidun_bg-img").bounding_box()
    if not slider or not background:
        return False
    x = slider["x"] + slider["width"] / 2
    y = slider["y"] + slider["height"] / 2
    page.mouse.move(x, y)
    page.mouse.down()
    try:
        page.mouse.move(x + 30, y)
        page.wait_for_timeout(150)
        panel_path = os.path.join(temporary, "panel.png")
        page.locator(".yidun_panel").screenshot(path=panel_path)
        piece = page.locator(".yidun_jigsaw").bounding_box()
        if not piece:
            return False
        current_left = piece["x"] - background["x"]
        gap, confidence = find_gap(panel_path, front_path, current_left, background["width"])
        if confidence < 0.55:
            return False
        human_to(page, x + 30, y, x + 30 + gap - current_left)
    finally:
        page.mouse.up()
    return True


def save_state(context, path, overwrite=False):
    """Publish a complete credential file, atomically refusing existing paths."""
    target = Path(path).absolute()
    if target.is_symlink():
        raise ValueError("State output must not be a symbolic link")
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".state-", suffix=".json", dir=target.parent)
    os.close(descriptor)
    try:
        context.storage_state(path=temporary)
        os.chmod(temporary, 0o600)
        if overwrite:
            os.replace(temporary, target)
        elif os.name == "nt":
            # Windows rename fails atomically if the target already exists.
            os.rename(temporary, target)
        else:
            # Same-directory hard link publishes atomically without clobbering a
            # state file another process created while interactive login waited.
            os.link(temporary, target)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--state", default=DEFAULT_STATE)
    parser.add_argument("--phone", help="Prefer ZHS_PHONE environment variable")
    parser.add_argument("--pwd", help="Prefer ZHS_PWD; CLI values appear in process lists")
    parser.add_argument("--credentials", help="Optional JSON file, read only if needed")
    parser.add_argument(
        "--timeout", type=float, default=180, help="Interactive login timeout, seconds"
    )
    parser.add_argument(
        "--auto-captcha", action="store_true", help="Opt in to the optional legacy slider solver"
    )
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument(
        "--overwrite-state", action="store_true", help="Explicitly replace an existing state file"
    )
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.rounds < 1:
        parser.error("--timeout and --rounds must be positive")
    try:
        service = urlsplit(args.service)
    except ValueError:
        parser.error("--service is not a valid URL")
    kind = service_kind(service.hostname)
    if service.scheme != "https" or not service.hostname or kind is None:
        parser.error("--service must be an HTTPS Zhihuishu, Chaoxing or icourse163 URL")
    if kind in {"chaoxing", "icourse163"} and (
        args.auto_captcha or args.phone or args.pwd or args.credentials
    ):
        parser.error(
            "Credential filling and auto-captcha apply to Zhihuishu only; "
            "complete the " + kind + " login manually"
        )
    if os.path.exists(args.state) and not args.overwrite_state:
        print("State file already exists; select a different path or use --overwrite-state")
        return 1
    phone = password = None
    try:
        phone, password = load_credentials(args)
        if args.auto_captcha:
            # Fail early before opening a browser if optional solver dependencies are absent.
            try:
                import cv2  # noqa: F401
                import numpy  # noqa: F401
            except ImportError as exc:
                raise RuntimeError(
                    "--auto-captcha requires the captcha optional dependency group"
                ) from exc
            if not (phone and password):
                raise ValueError("--auto-captcha requires phone and password credentials")
        if args.pwd:
            print("CLI passwords are visible in process lists; prefer ZHS_PWD")
        login_url = LOGIN_BASE + quote(args.service, safe="")
        state_directory = Path(args.state).absolute().parent
        state_directory.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".login-", dir=state_directory) as temporary:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(
                    headless=False,
                    args=[
                        "--disable-blink-features=AutomationControlled",
                        "--window-size=1600,1000",
                    ],
                )
                try:
                    context = browser.new_context(
                        locale="zh-CN", viewport={"width": 1600, "height": 1000}
                    )
                    context.add_init_script(
                        "Object.defineProperty(navigator,'webdriver',{get:()=>false});"
                    )
                    page = context.new_page()
                    if kind in {"chaoxing", "icourse163"}:
                        # The platform redirects to its own passport and back to
                        # the service page; the login form itself is never touched.
                        page.goto(args.service, wait_until="domcontentloaded", timeout=60000)
                        print(
                            "Complete login in the browser window; the page returns to the course"
                        )
                        success = wait_for_login(page, args.service, args.timeout)
                    elif args.auto_captcha:
                        capture = ChallengeCapture()
                        page.on("response", capture.on_response)
                        success = False
                        for _ in range(args.rounds):
                            capture.body = None
                            prepare_login(page, login_url, phone, password)
                            if is_logged_in(page, args.service):
                                success = True
                                break
                            try:
                                solve_challenge(page, capture, temporary)
                            except Exception as exc:
                                print(
                                    "Optional solver failed: " + login_error(exc, phone, password)
                                )
                            if wait_for_login(page, args.service, min(args.timeout, 10)):
                                success = True
                                break
                    else:
                        prepare_login(page, login_url, phone, password)
                        print("Complete login and any challenge in the browser window")
                        success = wait_for_login(page, args.service, args.timeout)
                    if not success:
                        print("Login timed out; no state file created")
                        return 1
                    save_state(context, args.state, overwrite=args.overwrite_state)
                    print("Session state saved:", args.state)
                    print(
                        "This file contains reusable credentials. You own it; delete it after downloads."
                    )
                    return 0
                finally:
                    browser.close()
    except KeyboardInterrupt:
        print("Login interrupted; temporary files removed")
        return 1
    except Exception as exc:
        print("Login failed: " + login_error(exc, phone, password))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
