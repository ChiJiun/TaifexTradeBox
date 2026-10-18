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
SUBSCRIBE_ID = "btnSubscribe"
MESSAGES_TAB_ID = "tabPage1"
APPLY_SETTINGS_ID = "btnApply"
# TTB's own "resubmit failed orders every 5 seconds" option would create
# orders the bot never acknowledged; the bot's ownership checks require it off.
AUTO_RESUBMIT_ID = "chkAutoPut"
AUTO_RECONNECT_ID = "chkReconnect"


class LoginError(RuntimeError):
    """Login could not be completed or verified."""


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


class TtbWindow:
    """Thin pywinauto wrapper; imported lazily so CI never needs a desktop."""

    def __init__(self, ttb_exe: Path, start_timeout: float) -> None:
        from pywinauto import Application  # type: ignore[import-not-found]
        from pywinauto.findwindows import ElementNotFoundError  # type: ignore[import-not-found]

        self._not_found = ElementNotFoundError
        try:
            self.app = Application(backend="uia").connect(path=str(ttb_exe), timeout=3)
        except Exception:
            subprocess.Popen([str(ttb_exe)], cwd=str(ttb_exe.parent))
            self.app = Application(backend="uia").connect(path=str(ttb_exe), timeout=start_timeout)
        self.main = self.app.window(auto_id=MAIN_FORM_ID)
        self.main.wait("exists", timeout=start_timeout)

    def control(self, auto_id: str, **criteria: Any) -> Any:
        return self.main.child_window(auto_id=auto_id, **criteria)

    def enabled(self, auto_id: str) -> bool:
        item = self.control(auto_id)
        return item.exists(timeout=1) and item.is_enabled()

    def enforce_settings(self) -> list[str]:
        """Turn off TTB auto-resubmit and keep auto-reconnect on (pre-login only)."""
        changes: list[str] = []
        tab = self.control(SETTINGS_TAB_ID, control_type="TabItem")
        if not tab.exists(timeout=2):
            return changes
        tab.select()
        for auto_id, wanted in ((AUTO_RESUBMIT_ID, 0), (AUTO_RECONNECT_ID, 1)):
            box = self.control(auto_id, control_type="CheckBox")
            if box.exists(timeout=2) and box.is_enabled() and box.get_toggle_state() != wanted:
                box.toggle()
                changes.append(f"{auto_id}={'on' if wanted else 'off'}")
        if changes and self.enabled(APPLY_SETTINGS_ID):
            self.control(APPLY_SETTINGS_ID).invoke()
        back = self.control(MESSAGES_TAB_ID, control_type="TabItem")
        if back.exists(timeout=1):
            back.select()
        return changes

    def fill_login(self, credential: Credential) -> None:
        self.control(ACCOUNT_ID).set_edit_text(credential.user)
        password = self.control(PASSWORD_ID)
        password.set_edit_text(credential.secret)

    def dialogs(self) -> list[str]:
        texts = []
        for window in self.app.windows():
            if window.element_info.automation_id != MAIN_FORM_ID:
                body = " ".join(child.window_text() for child in window.descendants() if child.window_text())
                texts.append((window.window_text() + " " + body).strip()[:200])
        return texts

    def dismiss_dialogs(self) -> None:
        for window in self.app.windows():
            if window.element_info.automation_id == MAIN_FORM_ID:
                continue
            for button in window.descendants(control_type="Button"):
                if button.window_text() in {"確定", "OK", "是", "Yes"}:
                    button.invoke()
                    break

    def competitions(self) -> list[str]:
        combo = self.control(GAME_LIST_ID, control_type="ComboBox")
        return [text for text in combo.texts() if text]

    def selected_competition(self) -> str:
        combo = self.control(GAME_LIST_ID, control_type="ComboBox")
        try:
            return combo.selected_text() or ""
        except Exception:
            return ""

    def select_competition(self, name: str) -> None:
        self.control(GAME_LIST_ID, control_type="ComboBox").select(name)

    def subscribe_quote(self, symbol: str) -> str:
        """Best effort: TTB only publishes ticks for symbols in 報價設定.

        The quote tab is hidden before login, so its symbol box was not
        inspectable offline; the first enabled Edit in that tab is used.
        """
        tab = self.control(QUOTES_TAB_ID, control_type="TabItem")
        if not tab.exists(timeout=2):
            return "quote_tab_missing"
        tab.select()
        pane = self.control(QUOTES_TAB_ID, control_type="Pane")
        if not pane.exists(timeout=3):
            return "quote_tab_unavailable"
        code = symbol.upper().strip()
        texts = {element.window_text().strip().upper() for element in pane.descendants()}
        if code in texts or any(text.startswith(code + " ") for text in texts):
            result = "already_subscribed"
        else:
            boxes = [edit for edit in pane.descendants(control_type="Edit") if edit.is_enabled()]
            if not boxes or not self.enabled(SUBSCRIBE_ID):
                return "subscribe_controls_not_found"
            boxes[0].set_edit_text(code)
            self.control(SUBSCRIBE_ID).invoke()
            result = "subscribed"
        back = self.control(MESSAGES_TAB_ID, control_type="TabItem")
        if back.exists(timeout=1):
            back.select()
        return result

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
) -> dict[str, Any]:
    first = probe()
    if first.logged_in:
        result = {"ok": True, "action": "already_logged_in", "session": first.as_dict()}
        if quote_symbols:
            try:
                window = window_factory()
            except Exception as exc:  # GUI tooling broken must not block a verified session
                result["quotes"] = {symbol: f"error:{type(exc).__name__}" for symbol in quote_symbols}
            else:
                result["quotes"] = _subscribe_all(window, quote_symbols)
        return result

    window = window_factory()
    actions: list[str] = []
    if not window.enabled(LOGOUT_ID):
        actions.extend(window.enforce_settings())
        credential = credential_reader(credential_target)
        window.fill_login(credential)
        actions.append("credentials_filled")
        if not submit:
            return {"ok": False, "action": "filled_without_submit", "steps": actions}
        window.control(LOGON_ID).invoke()
        actions.append("logon_clicked")
        if not wait_for(lambda: window.enabled(GAME_LIST_ID) or window.enabled(LOGOUT_ID), login_timeout):
            messages = window.dialogs()
            window.dismiss_dialogs()
            raise LoginError(f"TTB login did not complete; dialogs={messages}")

    items = wait_for(window.competitions, 20.0) or []
    target = choose_competition(list(items), competition)
    if window.selected_competition() != target:
        window.select_competition(target)
        actions.append("competition_selected")

    verified = wait_for(lambda: (lambda p: p if p.logged_in else None)(probe()), verify_timeout, 3.0)
    if not verified:
        last = probe()
        messages = window.dialogs()
        raise LoginError(
            f"TTB API still not logged in after selecting {target!r}: codes={last.codes} dialogs={messages}"
        )
    return {
        "ok": True, "action": "logged_in", "competition": target,
        "steps": actions, "session": verified.as_dict(),
        "quotes": _subscribe_all(window, quote_symbols),
    }


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
    parser.add_argument("--no-submit", action="store_true", help="fill the login form but do not press 登入")
    parser.add_argument("--dump-controls", action="store_true", help="print the TTB UI tree (password hidden)")
    parser.add_argument("--start-timeout", type=float, default=60.0)
    parser.add_argument("--subscribe", action="append", default=[],
                        help="contract code to keep in TTB 報價設定 (repeatable, best effort)")
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
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["ok"] else 4
    except LoginError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False),
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
