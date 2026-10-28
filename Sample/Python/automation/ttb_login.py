"""Unattended TTB GUI login and competition selection for the Azure VM.

Run from Sample/Python:

    python -m automation.ttb_login --check-only          # read-only API probe
    python -m automation.ttb_login --competition "校內"   # log in if needed

Credentials are read from Windows Credential Manager (generic credential,
default target ``TaifexTradeBox/TTB``), created once by the operator with:

    cmdkey /generic:TaifexTradeBox/TTB /user:<TTB e-mail> /pass

The password is never printed, logged or written to disk by this script.
UI Automation patterns (Value/Invoke/Toggle/Selection) are used instead of
mouse/keyboard input so login also works in a disconnected RDP session.
Control IDs were taken from TTB 2025.11.27.1900 (WinForms control names).
"""
from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ttb.session import SessionProbe, probe_session

DEFAULT_TARGET = "TaifexTradeBox/TTB"
MAIN_FORM_ID = "MainForm"
ACCOUNT_ID = "txtAccnt"
PASSWORD_ID = "txtPwd"
LOGON_ID = "btnLogOn"
LOGOUT_ID = "btnLogOut"
GAME_LIST_ID = "cbGameList"
SETTINGS_TAB_ID = "tabPage2"
QUOTES_TAB_ID = "tabPage3"
QUOTE_GRID_ID = "dvQuote"
MESSAGES_TAB_ID = "tabPage1"
APPLY_SETTINGS_ID = "btnApply"
# TTB's own "resubmit failed orders every 5 seconds" option would create
# orders the bot never acknowledged; the bot's ownership checks require it off.
AUTO_RESUBMIT_ID = "chkAutoPut"
AUTO_RECONNECT_ID = "chkReconnect"
# Order reports arrive only over TTB's report socket, which can be dead right
# after login; 權益數 刷新 clears the grid and waits for the server's answer.
EQUITY_TAB_ID = "tabPage5"
EQUITY_REFRESH_ID = "btnRefreshEquity"
EQUITY_GRID_ID = "dvEquity"

# Win32 messages used instead of blocking UI Automation calls.
BM_CLICK = 0x00F5
WM_CLOSE = 0x0010
WM_COMMAND = 0x0111
CB_SETCURSEL = 0x014E
CBN_SELCHANGE = 1


class LoginError(RuntimeError):
    """Login could not be completed or verified."""


class ReportChannelDead(LoginError):
    """TTB is logged in but its order-report socket does not answer; restart TTB."""


@dataclass(frozen=True)
class Credential:
    user: str
    secret: str

    def __repr__(self) -> str:  # never leak the secret through logs/tracebacks
        return f"Credential(user={self.user!r}, secret=<hidden>)"


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]


class _CREDENTIAL(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR),
        ("Comment", wintypes.LPWSTR),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD),
        ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def decode_secret(blob: bytes) -> str:
    """cmdkey stores generic secrets as UTF-16LE; tolerate UTF-8 writers."""
    if len(blob) % 2 == 0:
        try:
            text = blob.decode("utf-16-le")
            if text and all(ch.isprintable() for ch in text):
                return text
        except UnicodeDecodeError:
            pass
    return blob.decode("utf-8")


def read_credential(target: str) -> Credential:
    if sys.platform != "win32":
        raise LoginError("Windows Credential Manager is only available on Windows")
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.CredReadW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.POINTER(_CREDENTIAL)),
    ]
    advapi.CredReadW.restype = wintypes.BOOL
    advapi.CredFree.argtypes = [ctypes.c_void_p]
    pointer = ctypes.POINTER(_CREDENTIAL)()
    if not advapi.CredReadW(target, 1, 0, ctypes.byref(pointer)):  # CRED_TYPE_GENERIC
        raise LoginError(
            f"credential {target!r} not found; run: cmdkey /generic:{target} /user:<email> /pass"
        )
    try:
        cred = pointer.contents
        blob = ctypes.string_at(cred.CredentialBlob, cred.CredentialBlobSize)
        user = cred.UserName or ""
        secret = decode_secret(blob)
    finally:
        advapi.CredFree(pointer)
    if not user or not secret:
        raise LoginError(f"credential {target!r} has an empty user name or password")
    return Credential(user, secret)


def choose_competition(items: list[str], wanted: str) -> str:
    """Pick exactly one competition; never guess between several."""
    options = [item.strip() for item in items if item and item.strip()]
    if not options:
        raise LoginError("TTB competition list is empty; register for the competition on the web first")
    if wanted.strip():
        exact = [item for item in options if item == wanted.strip()]
        if exact:
            return exact[0]
        partial = [item for item in options if wanted.strip().lower() in item.lower()]
        if len(partial) == 1:
            return partial[0]
        raise LoginError(
            f"competition {wanted!r} matched {len(partial)} entries; available: {options}"
        )
    if len(options) == 1:
        return options[0]
    raise LoginError(f"several competitions available; set ttbCompetition to one of {options}")


def wait_for(predicate: Callable[[], Any], timeout: float, interval: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        try:
            value = predicate()
            if value:
                return value
        except Exception:
            value = None
        if time.monotonic() >= deadline:
            return value
        time.sleep(interval)


def default_probe(host: str, zmq_port: int) -> SessionProbe:
    try:
        from ttb.api import TTBApi
        api = TTBApi.connect(host, zmq_port)
    except Exception as exc:
        probe = SessionProbe()
        probe.error = type(exc).__name__
        return probe
    return probe_session(api)


def reveal_main_window(process_id: int) -> bool:
    """Show TTB's MainForm if it is hidden in the tray; True once it is visible."""
    from pywinauto import Desktop  # type: ignore[import-not-found]

    user32 = ctypes.windll.user32
    for window in Desktop(backend="win32").windows(visible_only=False, enabled_only=False):
        if window.process_id() != process_id or not window.window_text().startswith("TTB-Ver"):
            continue
        if not user32.IsWindowVisible(window.handle) or user32.IsIconic(window.handle):
            user32.ShowWindow(window.handle, 9)  # SW_RESTORE
        return bool(user32.IsWindowVisible(window.handle))
    return False


class TtbWindow:
    """Thin pywinauto wrapper; imported lazily so CI never needs a desktop."""

    def __init__(self, ttb_exe: Path, start_timeout: float) -> None:
        from pywinauto import Application  # type: ignore[import-not-found]
        from pywinauto.findwindows import ElementNotFoundError  # type: ignore[import-not-found]

        self._not_found = ElementNotFoundError
        try:
            process = Application(backend="win32").connect(path=str(ttb_exe), timeout=3).process
        except Exception:
            subprocess.Popen([str(ttb_exe)], cwd=str(ttb_exe.parent))
            process = Application(backend="win32").connect(path=str(ttb_exe), timeout=start_timeout).process
        # TTB hides MainForm when minimized to the tray, and UI Automation does
        # not expose hidden windows; restore it (no input is sent).
        wait_for(lambda: reveal_main_window(process), start_timeout)
        self.process_id = process
        self.app = Application(backend="uia").connect(process=process, timeout=start_timeout)
        self.main = self.app.window(auto_id=MAIN_FORM_ID)
        self.main.wait("exists", timeout=start_timeout)

    def control(self, auto_id: str, **criteria: Any) -> Any:
        return self.main.child_window(auto_id=auto_id, **criteria)

    def enabled(self, auto_id: str) -> bool:
        item = self.control(auto_id)
        return item.exists(timeout=1) and item.is_enabled()

    def _hwnd(self, auto_id: str) -> int:
        return int(self.control(auto_id).handle)

    def press(self, auto_id: str) -> None:
        """Click via PostMessage: TTB opens modal message boxes from button
        handlers, and a UIA Invoke would block until someone closes them."""
        ctypes.windll.user32.PostMessageW(self._hwnd(auto_id), BM_CLICK, 0, 0)

    def _settings_tab(self) -> None:
        tab = self.control(SETTINGS_TAB_ID, control_type="TabItem")
        if not tab.exists(timeout=2):
            raise LoginError(f"TTB settings tab {SETTINGS_TAB_ID} not found; cannot confirm {AUTO_RESUBMIT_ID} is off")
        tab.select()

    def _back_to_messages(self) -> None:
        back = self.control(MESSAGES_TAB_ID, control_type="TabItem")
        if back.exists(timeout=1):
            back.select()

    def auto_resubmit_state(self) -> str:
        """Read-only: "off", "on" or "unknown" for TTB's 失敗自動重送 checkbox."""
        try:
            self._settings_tab()
            box = self.control(AUTO_RESUBMIT_ID, control_type="CheckBox")
            if not box.exists(timeout=2):
                return "unknown"
            # WinForms draws these itself, so BM_GETCHECK is not reliable; UIA Toggle is.
            return {0: "off", 1: "on"}.get(box.get_toggle_state(), "unknown")
        except LoginError:
            return "unknown"
        finally:
            self._back_to_messages()

    def enforce_settings(self) -> list[str]:
        """Turn off TTB auto-resubmit (required) and keep auto-reconnect on (best effort).

        Raises LoginError whenever auto-resubmit cannot be switched off and
        applied: TTB would otherwise resend failed orders the bot never placed.
        """
        changes: list[str] = []
        self._settings_tab()
        try:
            for auto_id, wanted, required in ((AUTO_RESUBMIT_ID, 0, True), (AUTO_RECONNECT_ID, 1, False)):
                box = self.control(auto_id, control_type="CheckBox")
                if not box.exists(timeout=2):
                    if required:
                        raise LoginError(f"{auto_id} not found in TTB settings")
                    continue
                if box.get_toggle_state() == wanted:
                    continue
                if not box.is_enabled():
                    if required:
                        raise LoginError(f"{auto_id} is on and disabled; cannot switch it off")
                    continue
                box.toggle()
                if not wait_for(lambda: box.get_toggle_state() == wanted, 5.0, 0.2):
                    raise LoginError(f"could not set {auto_id}")
                changes.append(f"{auto_id}={'on' if wanted else 'off'}")
            if changes:
                if not self.enabled(APPLY_SETTINGS_ID):
                    raise LoginError(f"{APPLY_SETTINGS_ID} disabled; settings {changes} were not applied")
                self.press(APPLY_SETTINGS_ID)
                # TTB confirms with a modal "設定成功" message box; close it.
                # Anything else (e.g. 設定失敗) means the change did not stick.
                replies = wait_for(self.dismiss_dialogs, 5.0, 0.5) or []
                if any("成功" not in text for text in replies):
                    raise LoginError(f"TTB rejected settings {changes}: {replies}")
                changes.extend(f"dialog:{text}" for text in replies)
        finally:
            self._back_to_messages()
        return changes

    def report_channel_alive(self, timeout: float = 15.0) -> bool:
        tab = self.control(EQUITY_TAB_ID, control_type="TabItem")
        if not tab.exists(timeout=2):
            raise LoginError(f"TTB equity tab {EQUITY_TAB_ID} not found")
        tab.select()
        try:
            grid = self.control(EQUITY_GRID_ID, control_type="DataGrid")
            rows = lambda: sum(row.element_info.name.startswith("Row") for row in grid.descendants())
            self.press(EQUITY_REFRESH_ID)
            # Old rows still on screen would read as an answer; wait for the clear first.
            if not wait_for(lambda: rows() == 0, 5.0, 0.2):
                return False
            return bool(wait_for(rows, timeout))
        finally:
            self._back_to_messages()

    def fill_login(self, credential: Credential) -> None:
        self.control(ACCOUNT_ID).set_edit_text(credential.user)
        password = self.control(PASSWORD_ID)
        password.set_edit_text(credential.secret)

    def _dialog_windows(self) -> list[Any]:
        from pywinauto import Desktop  # type: ignore[import-not-found]

        return [
            window for window in Desktop(backend="win32").windows(visible_only=True)
            if window.process_id() == self.process_id and window.class_name() == "#32770"
        ]

    @staticmethod
    def _dialog_text(window: Any) -> str:
        body = " ".join(child.window_text() for child in window.children() if child.window_text())
        return (window.window_text() + " " + body).strip()[:200]

    def dialogs(self) -> list[str]:
        return [self._dialog_text(window) for window in self._dialog_windows()]

    def dismiss_dialogs(self) -> list[str]:
        """Close TTB message boxes with their OK button; return their texts."""
        closed = []
        for window in self._dialog_windows():
            closed.append(self._dialog_text(window))
            buttons = [child for child in window.children()
                       if child.class_name() == "Button" and child.window_text() in {"確定", "OK", "是", "Yes"}]
            target = buttons[0].handle if buttons else window.handle
            ctypes.windll.user32.PostMessageW(target, BM_CLICK if buttons else WM_CLOSE, 0, 0)
        return closed

    def _combo(self) -> Any:
        from pywinauto.controls.win32_controls import ComboBoxWrapper  # type: ignore[import-not-found]

        return ComboBoxWrapper(self._hwnd(GAME_LIST_ID))

    def competitions(self) -> list[str]:
        return [text for text in self._combo().item_texts() if text]

    def selected_competition(self) -> str:
        combo = self._combo()
        index = combo.selected_index()
        items = combo.item_texts()
        return items[index] if 0 <= index < len(items) else ""

    def select_competition(self, name: str) -> None:
        """CB_SETCURSEL plus a posted CBN_SELCHANGE, which WinForms turns into
        SelectedIndexChanged (TTB's GameEnter) without blocking on dialogs."""
        combo = self._combo()
        index = combo.item_texts().index(name)
        user32 = ctypes.windll.user32
        hwnd = int(combo.handle)
        user32.SendMessageW(hwnd, CB_SETCURSEL, index, 0)
        control_id = user32.GetDlgCtrlID(hwnd)
        user32.PostMessageW(user32.GetParent(hwnd), WM_COMMAND, (CBN_SELCHANGE << 16) | (control_id & 0xFFFF), hwnd)

    def subscribe_quote(self, symbol: str) -> str:
        """Check (read-only) that symbol is in TTB's 報價設定 grid.

        TTB only publishes ticks for subscribed symbols. The list survives a
        TTB restart, and 新增訂閱 opens a dialog, so this does not add symbols:
        a missing contract is reported for the operator to add by hand.
        """
        tab = self.control(QUOTES_TAB_ID, control_type="TabItem")
        if not tab.exists(timeout=2):
            return "quote_tab_missing"
        tab.select()
        try:
            grid = self.control(QUOTE_GRID_ID, control_type="DataGrid")
            if not grid.exists(timeout=3):
                return "quote_grid_missing"
            subscribed = {
                str(cell.legacy_properties().get("Value", "")).strip().upper()
                for cell in grid.descendants(control_type="DataItem")
                if cell.element_info.name.startswith("商品代號")
            }
        finally:
            back = self.control(MESSAGES_TAB_ID, control_type="TabItem")
            if back.exists(timeout=1):
                back.select()
        return "subscribed" if symbol.upper().strip() in subscribed else "NOT_SUBSCRIBED_add_in_TTB_quote_settings"

    def dump(self) -> list[str]:
        rows = []
        for element in self.main.descendants():
            info = element.element_info
            text = "" if info.automation_id == PASSWORD_ID else element.window_text()[:40]
            rows.append(f"{info.control_type} id={info.automation_id!r} text={text!r} enabled={info.enabled}")
        return rows


def ensure_logged_in(
    *,
    competition: str,
    credential_target: str,
    ttb_exe: Path,
    probe: Callable[[], SessionProbe],
    window_factory: Callable[[], Any],
    credential_reader: Callable[[str], Credential] = read_credential,
    login_timeout: float = 60.0,
    verify_timeout: float = 45.0,
    submit: bool = True,
    quote_symbols: tuple[str, ...] = (),
    require_auto_resubmit_off: bool = False,
    require_report_channel: bool = False,
) -> dict[str, Any]:
    first = probe()
    if first.logged_in:
        result = {"ok": True, "action": "already_logged_in", "session": first.as_dict()}
        if require_auto_resubmit_off or require_report_channel:
            # Live orders: never trust a session someone else logged in (and
            # possibly left 失敗自動重送 on); GUI errors fail closed.
            try:
                window = window_factory()
            except Exception as exc:
                raise LoginError(f"cannot open TTB GUI to confirm live settings: "
                                 f"{type(exc).__name__}: {exc}") from exc
            if require_auto_resubmit_off:
                result["steps"] = _confirm_auto_resubmit_off(window)
            result["quotes"] = _subscribe_all(window, quote_symbols)
            if require_report_channel:
                _require_report_channel(window)
            return result
        if quote_symbols:
            try:
                window = window_factory()
            except Exception as exc:  # GUI tooling broken must not block a verified session
                result["quotes"] = {symbol: f"error:{type(exc).__name__}" for symbol in quote_symbols}
            else:
                result["quotes"] = _subscribe_all(window, quote_symbols)
        return result

    window = window_factory()
    # A message box left open (e.g. by an earlier attempt) blocks TTB's UI.
    actions: list[str] = [f"dialog:{text}" for text in window.dismiss_dialogs()]
    if not window.enabled(LOGOUT_ID):
        actions.extend(window.enforce_settings())
        credential = credential_reader(credential_target)
        window.fill_login(credential)
        actions.append("credentials_filled")
        if not submit:
            return {"ok": False, "action": "filled_without_submit", "steps": actions}
        window.press(LOGON_ID)
        actions.append("logon_clicked")
        if not wait_for(lambda: window.enabled(GAME_LIST_ID) or window.enabled(LOGOUT_ID), login_timeout):
            raise LoginError(f"TTB login did not complete; dialogs={window.dismiss_dialogs()}")
        actions.extend(f"dialog:{text}" for text in window.dismiss_dialogs())

    items = wait_for(window.competitions, 20.0) or []
    target = choose_competition(list(items), competition)
    if window.selected_competition() != target:
        window.select_competition(target)
        actions.append("competition_selected")
        actions.extend(f"dialog:{text}" for text in wait_for(window.dismiss_dialogs, 3.0) or [])

    verified = wait_for(lambda: (lambda p: p if p.logged_in else None)(probe()), verify_timeout, 3.0)
    if not verified:
        last = probe()
        messages = window.dismiss_dialogs()
        raise LoginError(
            f"TTB API still not logged in after selecting {target!r}: codes={last.codes} dialogs={messages}"
        )
    if require_auto_resubmit_off:
        # The pre-login step is skipped when the GUI already showed 登出.
        actions.extend(_confirm_auto_resubmit_off(window))
    if require_report_channel:
        _require_report_channel(window)
    return {
        "ok": True, "action": "logged_in", "competition": target,
        "steps": actions, "session": verified.as_dict(),
        "quotes": _subscribe_all(window, quote_symbols),
    }


def _confirm_auto_resubmit_off(window: Any) -> list[str]:
    """Switch 失敗自動重送 off if needed and re-read it; raise unless it reads off."""
    if window.auto_resubmit_state() == "off":
        return []
    steps = window.enforce_settings()
    # A late 設定成功 box would block the tab switch in the re-read below.
    steps.extend(f"dialog:{text}" for text in window.dismiss_dialogs())
    state = window.auto_resubmit_state()
    if state != "off":
        raise LoginError(f"{AUTO_RESUBMIT_ID} still {state} after enforcing settings; refusing live trading")
    return steps


def _require_report_channel(window: Any) -> None:
    if not window.report_channel_alive():
        raise ReportChannelDead("TTB report channel did not answer an equity refresh; restart TTB")


def _subscribe_all(window: Any, symbols: tuple[str, ...]) -> dict[str, str]:
    results = {}
    for symbol in symbols:
        try:
            results[symbol] = window.subscribe_quote(symbol)
        except Exception as exc:  # never fail a verified login over quotes
            results[symbol] = f"error:{type(exc).__name__}"
    return results


def _config_value(path: Path, key: str) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8")).get(key, "")
    except (OSError, ValueError, AttributeError):
        return ""
    return value if isinstance(value, str) else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Log TTB in to the configured competition (no orders are sent)")
    parser.add_argument("--config", type=Path, default=Path("automation/config.json"))
    parser.add_argument("--competition", default=None, help="competition name; default config ttbCompetition")
    parser.add_argument("--credential-target", default=None,
                        help=f"Credential Manager target; default config ttbCredentialTarget or {DEFAULT_TARGET}")
    parser.add_argument("--ttb-exe", type=Path, default=Path(r"C:\TTB\TTB.exe"))
    parser.add_argument("--host", default="http://localhost:8080")
    parser.add_argument("--zmq-port", type=int, default=51141)
    parser.add_argument("--check-only", action="store_true", help="read-only API probe; never touches the GUI")
    parser.add_argument("--no-submit", action="store_true", help="fill the login form but do not press the logon button (btnLogOn)")
    parser.add_argument("--dump-controls", action="store_true", help="print the TTB UI tree (password hidden)")
    parser.add_argument("--start-timeout", type=float, default=60.0)
    parser.add_argument("--subscribe", action="append", default=[],
                        help="contract code that must be in the TTB quote-settings grid (dvQuote); checked read-only and reported (repeatable)")
    parser.add_argument("--require-auto-resubmit-off", action="store_true",
                        help="fail unless TTB auto-resubmit (chkAutoPut) reads off, even when already logged in (live mode)")
    parser.add_argument("--require-report-channel", action="store_true",
                        help="exit 5 unless an equity refresh comes back over TTB's report socket (live mode)")
    args = parser.parse_args(argv)

    competition = args.competition if args.competition is not None else _config_value(args.config, "ttbCompetition")
    target = args.credential_target or _config_value(args.config, "ttbCredentialTarget") or DEFAULT_TARGET
    probe = lambda: default_probe(args.host, args.zmq_port)
    try:
        if args.check_only:
            result = probe()
            print(json.dumps({"ok": result.logged_in, "session": result.as_dict()}, ensure_ascii=False))
            return 0 if result.logged_in else 3
        if args.dump_controls:
            print("\n".join(TtbWindow(args.ttb_exe, args.start_timeout).dump()))
            return 0
        result = ensure_logged_in(
            competition=competition,
            credential_target=target,
            ttb_exe=args.ttb_exe,
            probe=probe,
            window_factory=lambda: TtbWindow(args.ttb_exe, args.start_timeout),
            submit=not args.no_submit,
            quote_symbols=tuple(symbol.upper() for symbol in args.subscribe if symbol.strip()),
            require_auto_resubmit_off=args.require_auto_resubmit_off,
            require_report_channel=args.require_report_channel,
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 4
    except ReportChannelDead as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 5
    except LoginError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
