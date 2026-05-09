import base64
import calendar
import json
import os
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from tkinter import (
    BOTH,
    BOTTOM,
    END,
    LEFT,
    RIGHT,
    TOP,
    Button,
    Entry,
    Frame,
    Label,
    LabelFrame,
    Listbox,
    messagebox,
    Scrollbar,
    Spinbox,
    StringVar,
    Tk,
    ttk,
)

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover - handled at runtime for users.
    Fernet = None
    InvalidToken = Exception

try:
    import holidays
except ImportError:  # pragma: no cover
    holidays = None

try:
    from plyer import notification
except ImportError:  # pragma: no cover
    notification = None


APP_TITLE = "Advanced Annual Leave Management System"
DATA_FILE = Path("alms_secure.dat")
KEY_FILE = Path("alms_secure.key")
DATE_FMT = "%Y-%m-%d"
WORK_HOURS_PER_DAY = 8


LEAVE_TYPES = [
    "연차휴가(유급)",
    "병가(유급/부분유급)",
    "무급병가",
    "결혼휴가(유급)",
    "배우자 출산휴가(유급)",
    "사망휴가(유급)",
    "장기근속자 포상",
    "불임치료휴가(부분유급)",
    "부모님 생신휴가(유급)",
    "예비군훈련휴가",
    "군복무휴가(무급)",
    "가족돌봄휴가(무급)",
    "무급휴가",
]

LEAVE_MODES = ["전일", "오전반차", "오후반차"]
LEAVE_STATUS = ["계획", "실시완료"]
RIGHT_PANEL_WIDTH = 360


def parse_date(value: str) -> date:
    return datetime.strptime(value.strip(), DATE_FMT).date()


def date_to_str(value: date) -> str:
    return value.strftime(DATE_FMT)


def ensure_dependency_ready() -> None:
    missing = []
    if Fernet is None:
        missing.append("cryptography")
    if holidays is None:
        missing.append("holidays")
    if missing:
        msg = "필수 라이브러리가 없습니다.\n\n설치 명령:\npy -m pip install " + " ".join(missing)
        messagebox.showerror("라이브러리 필요", msg)
        raise SystemExit(msg)


class AuthManager:
    """사용자 계정과 휴가 데이터를 암호화된 .dat 파일로 저장한다."""

    def __init__(self, data_file: Path = DATA_FILE, key_file: Path = KEY_FILE):
        ensure_dependency_ready()
        self.data_file = data_file
        self.key_file = key_file
        self.fernet = Fernet(self._load_or_create_key())
        self.data = self._load_data()

    def _load_or_create_key(self) -> bytes:
        if self.key_file.exists():
            return self.key_file.read_bytes()
        key = Fernet.generate_key()
        self.key_file.write_bytes(key)
        try:
            os.chmod(self.key_file, 0o600)
        except OSError:
            pass
        return key

    def _load_data(self) -> dict:
        if not self.data_file.exists():
            return {"version": "1.01", "users": {}}
        try:
            decrypted = self.fernet.decrypt(self.data_file.read_bytes())
            return json.loads(decrypted.decode("utf-8"))
        except (InvalidToken, json.JSONDecodeError, OSError) as exc:
            backup = self.data_file.with_suffix(".dat.broken")
            try:
                self.data_file.replace(backup)
            except OSError:
                pass
            messagebox.showwarning(
                "데이터 복구 안내",
                "암호화 데이터 파일을 읽을 수 없어 손상 파일로 분리했습니다.\n"
                f"백업 위치: {backup}\n\n새 데이터 파일을 생성합니다.\n원인: {exc}",
            )
            return {"version": "1.01", "users": {}}

    def save(self) -> None:
        payload = json.dumps(self.data, ensure_ascii=False, indent=2).encode("utf-8")
        self.data_file.write_bytes(self.fernet.encrypt(payload))

    def _hash_password(self, password: str, salt: Optional[bytes] = None) -> tuple[str, str]:
        import hashlib

        salt = salt or os.urandom(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 210_000)
        return base64.b64encode(salt).decode("ascii"), base64.b64encode(digest).decode("ascii")

    def register(self, username: str, password: str, name: str, hire_date: str) -> None:
        username = username.strip()
        name = name.strip()
        if not username or not password or not name:
            raise ValueError("사번, 비밀번호, 이름은 필수입니다.")
        parsed_hire_date = parse_date(hire_date)
        if parsed_hire_date > date.today():
            raise ValueError("입사일자는 오늘보다 미래일 수 없습니다.")
        if username in self.data["users"]:
            raise ValueError("이미 등록된 사번입니다.")

        salt, password_hash = self._hash_password(password)
        self.data["users"][username] = {
            "username": username,
            "password_salt": salt,
            "password_hash": password_hash,
            "name": name,
            "hire_date": date_to_str(parsed_hire_date),
            "manual_entitlement_days": None,
            "leave_records": {},
            "memos": {},
        }
        self.save()

    def login(self, username: str, password: str) -> dict:
        user = self.data["users"].get(username.strip())
        if not user:
            raise ValueError("등록되지 않은 사번입니다.")
        salt = base64.b64decode(user["password_salt"])
        _, candidate_hash = self._hash_password(password, salt)
        if candidate_hash != user["password_hash"]:
            raise ValueError("비밀번호가 올바르지 않습니다.")
        return user

    def update_user(self, user: dict) -> None:
        self.data["users"][user["username"]] = user
        self.save()


@dataclass
class FiscalYear:
    start: date
    end: date


class LeaveEngine:
    """입사일 기준 회계연도와 연차 부여/사용 시간을 계산한다."""

    def __init__(self, user: dict):
        self.user = user
        self.hire_date = parse_date(user["hire_date"])
        self.kr_holidays = holidays.KR()

    def years_of_service(self, today: Optional[date] = None) -> int:
        today = today or date.today()
        years = today.year - self.hire_date.year
        if (today.month, today.day) < (self.hire_date.month, self.hire_date.day):
            years -= 1
        return max(0, years)

    def fiscal_year(self, today: Optional[date] = None) -> FiscalYear:
        today = today or date.today()
        start_year = today.year
        anniversary = self._safe_anniversary(start_year)
        if anniversary > today:
            anniversary = self._safe_anniversary(start_year - 1)
        next_anniversary = self._safe_anniversary(anniversary.year + 1)
        return FiscalYear(anniversary, next_anniversary - timedelta(days=1))

    def _safe_anniversary(self, year: int) -> date:
        try:
            return date(year, self.hire_date.month, self.hire_date.day)
        except ValueError:
            return date(year, 2, 28)

    def estimated_entitlement_days(self, today: Optional[date] = None) -> float:
        today = today or date.today()
        years = self.years_of_service(today)
        if years < 1:
            months = (today.year - self.hire_date.year) * 12 + today.month - self.hire_date.month
            if today.day < self.hire_date.day:
                months -= 1
            return float(max(0, min(11, months)))
        return float(min(25, 15 + max(0, (years - 1) // 2)))

    def entitlement_days(self) -> float:
        manual = self.user.get("manual_entitlement_days")
        if manual is None:
            return self.estimated_entitlement_days()
        return float(manual)

    def mode_hours(self, mode: str) -> float:
        return WORK_HOURS_PER_DAY if mode == "전일" else WORK_HOURS_PER_DAY / 2

    def annual_leave_hours_used(self, status: Optional[str] = None) -> float:
        total = 0.0
        fy = self.fiscal_year()
        for day_key, records in self.user.get("leave_records", {}).items():
            day = parse_date(day_key)
            if not (fy.start <= day <= fy.end):
                continue
            for record in records:
                if record.get("type") == "연차휴가(유급)" and (status is None or record.get("status") == status):
                    total += self.mode_hours(record.get("mode", "전일"))
        return total

    def remaining_hours(self) -> float:
        return self.entitlement_days() * WORK_HOURS_PER_DAY - self.annual_leave_hours_used()

    def usage_percent(self) -> float:
        entitlement = self.entitlement_days() * WORK_HOURS_PER_DAY
        return 0.0 if entitlement <= 0 else min(999.0, self.annual_leave_hours_used() / entitlement * 100)

    def is_holiday(self, day: date) -> bool:
        return day in self.kr_holidays

    def holiday_name(self, day: date) -> str:
        return self.kr_holidays.get(day, "")

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5

    def is_in_fiscal_year(self, day: date) -> bool:
        fy = self.fiscal_year()
        return fy.start <= day <= fy.end


class NotificationService:
    """계획된 휴가를 앱 실행 중 팝업 또는 시스템 알림으로 알려준다."""

    def __init__(self, user: dict):
        self.user = user
        self.running = False
        self.thread: Optional[threading.Thread] = None
        self.alerted: set[str] = set()

    def start(self) -> None:
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.running = False

    def _loop(self) -> None:
        while self.running:
            today_key = date_to_str(date.today())
            tomorrow_key = date_to_str(date.today() + timedelta(days=1))
            for day_key in (today_key, tomorrow_key):
                if day_key in self.alerted:
                    continue
                records = self.user.get("leave_records", {}).get(day_key, [])
                planned = [r for r in records if r.get("status") == "계획"]
                if planned:
                    self.alerted.add(day_key)
                    text = f"{day_key} 예정 휴가 {len(planned)}건이 있습니다."
                    self.notify("ALMS 휴가 미리알림", text)
            time.sleep(3600)

    def notify(self, title: str, message: str) -> None:
        if notification is not None:
            try:
                notification.notify(title=title, message=message, timeout=8)
                return
            except Exception:
                pass
        try:
            messagebox.showinfo(title, message)
        except Exception:
            pass


class LoginWindow:
    def __init__(self, root: Tk, auth: AuthManager):
        self.root = root
        self.auth = auth
        self.on_success = None
        self.frame = Frame(root, padx=18, pady=18)
        self.frame.pack(fill=BOTH, expand=True)

        Label(self.frame, text="ALMS 로그인", font=("Malgun Gothic", 16, "bold")).grid(row=0, column=0, columnspan=2, pady=(0, 12))
        Label(self.frame, text="사번").grid(row=1, column=0, sticky="e", padx=5, pady=4)
        Label(self.frame, text="비밀번호").grid(row=2, column=0, sticky="e", padx=5, pady=4)
        Label(self.frame, text="이름").grid(row=3, column=0, sticky="e", padx=5, pady=4)
        Label(self.frame, text="입사일자").grid(row=4, column=0, sticky="e", padx=5, pady=4)

        self.username = StringVar()
        self.password = StringVar()
        self.name = StringVar()
        self.hire_date = StringVar()

        username_entry = Entry(self.frame, textvariable=self.username, width=28)
        username_entry.grid(row=1, column=1, pady=4)
        username_entry.bind("<FocusOut>", self.load_saved_profile)
        username_entry.bind("<Return>", self.load_saved_profile)
        Entry(self.frame, textvariable=self.password, show="*", width=28).grid(row=2, column=1, pady=4)
        Entry(self.frame, textvariable=self.name, width=28).grid(row=3, column=1, pady=4)
        Entry(self.frame, textvariable=self.hire_date, width=28).grid(row=4, column=1, pady=4)

        Button(self.frame, text="로그인", command=self.login).grid(row=5, column=0, pady=12, sticky="ew")
        Button(self.frame, text="신규 등록", command=self.register).grid(row=5, column=1, pady=12, sticky="ew")
        Label(self.frame, text="입사일자는 YYYY-MM-DD 형식입니다.", fg="#555").grid(row=6, column=0, columnspan=2)

    def load_saved_profile(self, _event=None) -> None:
        user = self.auth.data.get("users", {}).get(self.username.get().strip())
        if not user:
            return
        self.name.set(user.get("name", ""))
        self.hire_date.set(user.get("hire_date", ""))

    def login(self) -> None:
        try:
            user = self.auth.login(self.username.get(), self.password.get())
        except ValueError as exc:
            messagebox.showerror("로그인 실패", str(exc))
            return
        self.frame.destroy()
        if self.on_success:
            self.on_success(user)

    def register(self) -> None:
        try:
            self.auth.register(self.username.get(), self.password.get(), self.name.get(), self.hire_date.get())
        except ValueError as exc:
            messagebox.showerror("등록 실패", str(exc))
            return
        messagebox.showinfo("등록 완료", "사용자 등록이 완료되었습니다. 로그인해 주세요.")


class CalendarUI:
    def __init__(self, root: Tk, auth: AuthManager, user: dict):
        self.root = root
        self.auth = auth
        self.user = user
        self.engine = LeaveEngine(user)
        self.notifier = NotificationService(user)
        self.current_month = date.today().replace(day=1)
        self.selected_dates: set[date] = set()
        self.dragging = False
        self.day_widgets: dict[date, Frame] = {}

        self.leave_type = StringVar(value=LEAVE_TYPES[0])
        self.leave_mode = StringVar(value=LEAVE_MODES[0])
        self.leave_status = StringVar(value=LEAVE_STATUS[0])
        self.memo_text = StringVar()
        self.manual_days = StringVar(value=str(self.engine.entitlement_days()))

        self._build()
        self.refresh_all()
        self.notifier.start()

    def _build(self) -> None:
        self.root.title(APP_TITLE)
        self.root.geometry("1180x820")
        self.root.minsize(980, 720)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.top = Frame(self.root, padx=10, pady=8, bg="#f5f7fb")
        self.top.pack(side=TOP, fill="x")
        self.summary = Label(self.top, anchor="w", justify=LEFT, bg="#f5f7fb", font=("Malgun Gothic", 10, "bold"))
        self.summary.pack(side=LEFT, fill="x", expand=True)
        Button(self.top, text="PDF 출력", command=self.export_pdf).pack(side=RIGHT, padx=4)
        Button(self.top, text="저장", command=self.save).pack(side=RIGHT, padx=4)

        body = Frame(self.root)
        body.pack(fill=BOTH, expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, minsize=RIGHT_PANEL_WIDTH, weight=0)
        body.grid_rowconfigure(0, weight=1)

        left = Frame(body, padx=10, pady=10)
        left.grid(row=0, column=0, sticky="nsew")
        right = Frame(body, padx=10, pady=10, width=RIGHT_PANEL_WIDTH)
        right.grid(row=0, column=1, sticky="ns")
        right.grid_propagate(False)
        right.pack_propagate(False)

        nav = Frame(left)
        nav.pack(fill="x")
        Button(nav, text="<", width=4, command=lambda: self.shift_month(-1)).pack(side=LEFT)
        self.month_label = Label(nav, font=("Malgun Gothic", 16, "bold"))
        self.month_label.pack(side=LEFT, expand=True)
        Button(nav, text=">", width=4, command=lambda: self.shift_month(1)).pack(side=RIGHT)

        self.calendar_frame = Frame(left, bg="#d8dde8")
        self.calendar_frame.pack(fill=BOTH, expand=True, pady=(8, 8))

        list_frame = LabelFrame(left, text="선택일 상세 / 회계연도 전체 계획·메모")
        list_frame.pack(fill=BOTH, expand=False)
        self.detail_list = Listbox(list_frame, height=9)
        detail_scroll = Scrollbar(list_frame, orient="vertical", command=self.detail_list.yview)
        self.detail_list.configure(yscrollcommand=detail_scroll.set)
        self.detail_list.pack(side=LEFT, fill=BOTH, expand=True)
        detail_scroll.pack(side=RIGHT, fill="y")

        self._build_controls(right)

    def _build_controls(self, parent: Frame) -> None:
        entitlement = LabelFrame(parent, text="연차 보정")
        entitlement.pack(fill="x", pady=(0, 10))
        entitlement.grid_columnconfigure(1, weight=1)
        Label(entitlement, text="최종 연차 일수").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        Spinbox(entitlement, from_=0, to=40, increment=0.5, textvariable=self.manual_days, width=8).grid(row=0, column=1, sticky="ew", padx=6, pady=6)
        Button(entitlement, text="반영", command=self.apply_manual_days).grid(row=0, column=2, padx=6, pady=6)

        form = LabelFrame(parent, text="휴가 입력")
        form.pack(fill="x", pady=(0, 10))
        form.grid_columnconfigure(0, minsize=72)
        form.grid_columnconfigure(1, minsize=238, weight=1)
        Label(form, text="휴가종류").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(form, textvariable=self.leave_type, values=LEAVE_TYPES, state="readonly", width=26).grid(row=0, column=1, sticky="ew", padx=6, pady=4)
        Label(form, text="신청모드").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(form, textvariable=self.leave_mode, values=LEAVE_MODES, state="readonly", width=26).grid(row=1, column=1, sticky="ew", padx=6, pady=4)
        Label(form, text="상태").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        ttk.Combobox(form, textvariable=self.leave_status, values=LEAVE_STATUS, state="readonly", width=26).grid(row=2, column=1, sticky="ew", padx=6, pady=4)
        Button(form, text="휴가입력/수정", command=self.add_or_update_leave).grid(row=3, column=0, columnspan=2, sticky="ew", padx=6, pady=4)
        Button(form, text="선택 종류/모드 삭제", command=self.delete_leave).grid(row=4, column=0, columnspan=2, sticky="ew", padx=6, pady=4)

        memo = LabelFrame(parent, text="메모")
        memo.pack(fill="x", pady=(0, 10))
        Entry(memo, textvariable=self.memo_text, width=32).pack(fill="x", padx=6, pady=6)
        Button(memo, text="메모 추가/수정", command=self.upsert_memo).pack(fill="x", padx=6, pady=3)
        Button(memo, text="선택일 메모삭제", command=self.delete_memo).pack(fill="x", padx=6, pady=3)

        actions = LabelFrame(parent, text="선택")
        actions.pack(fill="x")
        Button(actions, text="선택 초기화", command=self.clear_selection).pack(fill="x", padx=6, pady=5)
        Button(actions, text="오늘로 이동", command=self.go_today).pack(fill="x", padx=6, pady=5)
        Label(actions, text="달력에서 클릭 또는 드래그로 날짜를 선택합니다.", fg="#555", wraplength=280).pack(fill="x", padx=6, pady=5)

    def shift_month(self, delta: int) -> None:
        month = self.current_month.month + delta
        year = self.current_month.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        self.current_month = date(year, month, 1)
        self.selected_dates.clear()
        self.refresh_all()

    def go_today(self) -> None:
        self.current_month = date.today().replace(day=1)
        self.selected_dates = {date.today()}
        self.refresh_all()

    def refresh_all(self) -> None:
        self.engine = LeaveEngine(self.user)
        self._draw_calendar()
        self._refresh_summary()
        self._refresh_details()

    def _refresh_summary(self) -> None:
        fy = self.engine.fiscal_year()
        entitlement = self.engine.entitlement_days()
        used = self.engine.annual_leave_hours_used()
        remain = self.engine.remaining_hours()
        today = date.today()
        self.summary.config(
            text=(
                f"이름: {self.user['name']} | 사번: {self.user['username']} | 입사일: {self.user['hire_date']} | "
                f"입사년차: {self.engine.years_of_service()}년 | 오늘: {date_to_str(today)}\n"
                f"회계연도: {date_to_str(fy.start)} ~ {date_to_str(fy.end)} | "
                f"총 연차: {entitlement:.1f}일/{entitlement * WORK_HOURS_PER_DAY:.0f}시간 | "
                f"사용/계획: {used / WORK_HOURS_PER_DAY:.1f}일/{used:.0f}시간 | "
                f"잔여: {remain / WORK_HOURS_PER_DAY:.1f}일/{remain:.0f}시간 | "
                f"권장 사용량 대비 사용률: {self.engine.usage_percent():.1f}%"
            )
        )

    def _draw_calendar(self) -> None:
        for child in self.calendar_frame.winfo_children():
            child.destroy()
        self.day_widgets.clear()
        self.month_label.config(text=f"{self.current_month.year}년 {self.current_month.month}월")

        weekdays = ["월", "화", "수", "목", "금", "토", "일"]
        for col, name in enumerate(weekdays):
            Label(self.calendar_frame, text=name, bg="#39465e", fg="white", font=("Malgun Gothic", 10, "bold")).grid(
                row=0, column=col, sticky="nsew", padx=1, pady=1
            )
            self.calendar_frame.columnconfigure(col, weight=1, uniform="cal")

        month_days = calendar.Calendar(firstweekday=0).monthdatescalendar(self.current_month.year, self.current_month.month)
        for row, week in enumerate(month_days, start=1):
            self.calendar_frame.rowconfigure(row, weight=1, uniform="cal")
            for col, day in enumerate(week):
                cell = self._make_day_cell(self.calendar_frame, day)
                cell.grid(row=row, column=col, sticky="nsew", padx=1, pady=1)

    def _make_day_cell(self, parent: Frame, day: date) -> Frame:
        records = self.user.get("leave_records", {}).get(date_to_str(day), [])
        memo = self.user.get("memos", {}).get(date_to_str(day), "")
        in_month = day.month == self.current_month.month
        in_fy = self.engine.is_in_fiscal_year(day)
        selected = day in self.selected_dates
        bg = self._day_bg(day, records, in_month, in_fy, selected)
        fg = "#111827" if in_month and in_fy else "#8a8f9c"

        cell = Frame(parent, bg=bg, highlightthickness=2 if selected else 0, highlightbackground="#1f6feb")
        day_label = Label(cell, text=str(day.day), anchor="nw", bg=bg, fg=fg, font=("Malgun Gothic", 10, "bold"))
        mark_label = Label(cell, text=self._day_mark(records, day), anchor="nw", justify=LEFT, bg=bg, fg="#18212f", font=("Malgun Gothic", 8))
        memo_label = Label(cell, text=memo[:10], anchor="sw", bg=bg, fg="#374151", font=("Malgun Gothic", 8))
        day_label.pack(fill="x", padx=4, pady=(3, 0))
        mark_label.pack(fill="x", padx=4)
        memo_label.pack(side=BOTTOM, fill="x", padx=4, pady=(0, 3))

        for widget in (cell, day_label, mark_label, memo_label):
            widget.bind("<Button-1>", lambda _event, d=day: self.on_day_press(d))
            widget.bind("<B1-Motion>", lambda event: self.on_day_drag(event))
            widget.bind("<ButtonRelease-1>", lambda _event: self.on_day_release())

        self.day_widgets[day] = cell
        return cell

    def _day_bg(self, day: date, records: list[dict], in_month: bool, in_fy: bool, selected: bool) -> str:
        if selected:
            return "#dbeafe"
        if not in_month or not in_fy:
            return "#eceff4"
        if self.engine.is_holiday(day):
            return "#fff6bf"
        if any(r.get("status") == "실시완료" and r.get("mode") == "전일" for r in records):
            return "#72b878"
        if any(r.get("status") == "계획" and r.get("mode") == "전일" for r in records):
            return "#c8f0c2"
        if any("반차" in r.get("mode", "") for r in records):
            return "#ead7ff"
        return "#ffffff"

    def _day_mark(self, records: list[dict], day: date) -> str:
        parts = []
        for record in records:
            mode = record.get("mode")
            if mode == "전일":
                parts.append("L")
            elif mode == "오전반차":
                parts.append("H-am")
            elif mode == "오후반차":
                parts.append("H-pm")
        if self.engine.is_holiday(day):
            parts.append(self.engine.holiday_name(day))
        return "\n".join(parts[:3])

    def _short_day_mark(self, records: list[dict], day: date, include_memo: bool = False) -> str:
        parts = []
        for record in records[:2]:
            mode = record.get("mode")
            if mode == "전일":
                parts.append("L")
            elif mode == "오전반차":
                parts.append("Ha")
            elif mode == "오후반차":
                parts.append("Hp")
        if self.engine.is_holiday(day):
            parts.append("Hol")
        if include_memo and self.user.get("memos", {}).get(date_to_str(day)):
            parts.append("M")
        return " ".join(parts[:3])

    def on_day_press(self, day: date) -> None:
        if not self.engine.is_in_fiscal_year(day):
            messagebox.showinfo("선택 불가", "현재 입사일 기준 회계연도 범위를 벗어난 날짜입니다.")
            return
        self.dragging = True
        if day in self.selected_dates:
            self.selected_dates.remove(day)
        else:
            self.selected_dates.add(day)
        self._refresh_day_cell(day)
        self._refresh_details()

    def on_day_drag(self, event) -> None:
        widget = event.widget.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            for day, cell in self.day_widgets.items():
                if widget == cell or widget.master == cell:
                    if self.engine.is_in_fiscal_year(day) and day not in self.selected_dates:
                        self.selected_dates.add(day)
                        self._refresh_day_cell(day)
                        self._refresh_details()
                    return
            widget = widget.master

    def on_day_release(self) -> None:
        self.dragging = False

    def _refresh_day_cell(self, day: date) -> None:
        cell = self.day_widgets.get(day)
        if cell is None:
            return
        records = self.user.get("leave_records", {}).get(date_to_str(day), [])
        in_month = day.month == self.current_month.month
        in_fy = self.engine.is_in_fiscal_year(day)
        selected = day in self.selected_dates
        bg = self._day_bg(day, records, in_month, in_fy, selected)
        cell.configure(bg=bg, highlightthickness=2 if selected else 0)
        self._set_child_backgrounds(cell, bg)

    def _set_child_backgrounds(self, widget, bg: str) -> None:
        for child in widget.winfo_children():
            try:
                child.configure(bg=bg)
            except Exception:
                pass
            self._set_child_backgrounds(child, bg)

    def _refresh_details(self) -> None:
        self.detail_list.delete(0, END)
        if self.selected_dates:
            self.detail_list.insert(END, "선택일 상세")
            for day in sorted(self.selected_dates):
                self._insert_day_detail(day, indent="  ")
            self.detail_list.insert(END, "")

        self.detail_list.insert(END, "회계연도 전체 계획·메모")
        scheduled_days = self._scheduled_days_in_fiscal_year()
        if not scheduled_days:
            self.detail_list.insert(END, "  등록된 휴가나 메모가 없습니다.")
            return
        first_current_month_index = None
        for day in scheduled_days:
            if first_current_month_index is None and day.year == self.current_month.year and day.month == self.current_month.month:
                first_current_month_index = self.detail_list.size()
            self._insert_day_detail(day, indent="  ")
        if first_current_month_index is not None:
            self.detail_list.see(first_current_month_index)

    def _scheduled_days_in_fiscal_year(self) -> list[date]:
        fy = self.engine.fiscal_year()
        day_keys = set(self.user.get("leave_records", {}).keys()) | set(self.user.get("memos", {}).keys())
        days = []
        for day_key in day_keys:
            try:
                day = parse_date(day_key)
            except ValueError:
                continue
            if fy.start <= day <= fy.end:
                days.append(day)
        return sorted(days)

    def _insert_day_detail(self, day: date, indent: str = "") -> None:
        day_key = date_to_str(day)
        weekend = " 주말" if self.engine.is_weekend(day) else ""
        holiday = f" 공휴일:{self.engine.holiday_name(day)}" if self.engine.is_holiday(day) else ""
        self.detail_list.insert(END, f"{indent}[{day_key}]{weekend}{holiday}")
        for record in self.user.get("leave_records", {}).get(day_key, []):
            hours = self.engine.mode_hours(record.get("mode", "전일"))
            self.detail_list.insert(END, f"{indent}  - {record.get('status')} | {record.get('type')} | {record.get('mode')} | {hours:g}시간")
        memo = self.user.get("memos", {}).get(day_key)
        if memo:
            self.detail_list.insert(END, f"{indent}  - 메모: {memo}")
        self.detail_list.see(END)

    def apply_manual_days(self) -> None:
        try:
            value = float(self.manual_days.get())
        except ValueError:
            messagebox.showerror("입력 오류", "연차 일수는 숫자로 입력해야 합니다.")
            return
        if value < 0 or (value * 2) % 1 != 0:
            messagebox.showerror("입력 오류", "연차 일수는 0.5일 단위의 양수여야 합니다.")
            return
        self.user["manual_entitlement_days"] = value
        self.save()
        self.refresh_all()

    def add_or_update_leave(self) -> None:
        if not self.selected_dates:
            messagebox.showerror("선택 필요", "달력에서 날짜를 먼저 선택하세요.")
            return
        warnings = [date_to_str(d) for d in self.selected_dates if self.engine.is_weekend(d) or self.engine.is_holiday(d)]
        if warnings and not messagebox.askyesno("휴일 경고", "주말 또는 공휴일이 포함되어 있습니다.\n" + "\n".join(warnings) + "\n계속 입력할까요?"):
            return
        for day in sorted(self.selected_dates):
            day_key = date_to_str(day)
            records = self.user.setdefault("leave_records", {}).setdefault(day_key, [])
            records[:] = [r for r in records if not (r.get("type") == self.leave_type.get() and r.get("mode") == self.leave_mode.get())]
            records.append({"type": self.leave_type.get(), "mode": self.leave_mode.get(), "status": self.leave_status.get()})
        self.selected_dates.clear()
        self.save()
        self.refresh_all()

    def delete_leave(self) -> None:
        for day in sorted(self.selected_dates):
            day_key = date_to_str(day)
            records = self.user.get("leave_records", {}).get(day_key, [])
            records[:] = [
                r
                for r in records
                if not (r.get("type") == self.leave_type.get() and r.get("mode") == self.leave_mode.get())
            ]
            if day_key in self.user.get("leave_records", {}) and not records:
                del self.user["leave_records"][day_key]
        self.selected_dates.clear()
        self.save()
        self.refresh_all()

    def upsert_memo(self) -> None:
        text = self.memo_text.get().strip()
        if len(text) > 10:
            messagebox.showerror("입력 오류", "메모는 10자 이내로 입력해야 합니다.")
            return
        if not self.selected_dates:
            messagebox.showerror("선택 필요", "메모를 넣을 날짜를 선택하세요.")
            return
        for day in self.selected_dates:
            self.user.setdefault("memos", {})[date_to_str(day)] = text
        self.memo_text.set("")
        self.save()
        self.refresh_all()

    def delete_memo(self) -> None:
        for day in self.selected_dates:
            self.user.get("memos", {}).pop(date_to_str(day), None)
        self.save()
        self.refresh_all()

    def clear_selection(self) -> None:
        self.selected_dates.clear()
        self.refresh_all()

    def save(self) -> None:
        self.auth.update_user(self.user)

    def export_pdf(self) -> None:
        try:
            from reportlab.lib import colors
            from reportlab.lib.pagesizes import A4, landscape
            from reportlab.lib.styles import getSampleStyleSheet
            from reportlab.pdfbase import pdfmetrics
            from reportlab.pdfbase.ttfonts import TTFont
            from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        except ImportError:
            messagebox.showerror("PDF 라이브러리 필요", "PDF 출력을 위해 reportlab이 필요합니다.\npy -m pip install reportlab")
            return

        docs = Path.home() / "Documents"
        docs.mkdir(exist_ok=True)
        fy = self.engine.fiscal_year()
        output = docs / f"ALMS_{self.user['username']}_{fy.start.year}_{fy.end.year}.pdf"
        doc = SimpleDocTemplate(str(output), pagesize=landscape(A4), rightMargin=18, leftMargin=18, topMargin=18, bottomMargin=18)
        styles = getSampleStyleSheet()
        pdf_font = self._register_pdf_font(pdfmetrics, TTFont)
        styles["Title"].fontName = pdf_font
        story = [
            Paragraph(f"ALMS Annual Calendar - {self.user['name']} ({date_to_str(fy.start)} ~ {date_to_str(fy.end)})", styles["Title"]),
            Spacer(1, 8),
        ]

        months = []
        cursor = fy.start.replace(day=1)
        while cursor <= fy.end:
            months.append(cursor)
            month = cursor.month + 1
            year = cursor.year + (month - 1) // 12
            cursor = date(year, (month - 1) % 12 + 1, 1)

        grid_rows = []
        for chunk_start in range(0, len(months), 4):
            row = []
            for month_date in months[chunk_start : chunk_start + 4]:
                row.append(self._month_pdf_table(month_date, Table, TableStyle, colors, pdf_font))
            grid_rows.append(row)
        table = Table(grid_rows, colWidths=[200] * 4, rowHeights=[168] * len(grid_rows))
        table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4)]))
        story.append(table)
        doc.build(story)
        messagebox.showinfo("PDF 저장 완료", f"Documents 폴더에 저장했습니다.\n{output}")

    def _register_pdf_font(self, pdfmetrics, TTFont) -> str:
        font_candidates = [
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "malgun.ttf",
            Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "malgunbd.ttf",
        ]
        for font_path in font_candidates:
            if font_path.exists():
                try:
                    pdfmetrics.registerFont(TTFont("MalgunGothic", str(font_path)))
                    return "MalgunGothic"
                except Exception:
                    continue
        return "Helvetica"

    def _month_pdf_table(self, month_date: date, Table, TableStyle, colors, pdf_font: str):
        data = [[f"{month_date.year}-{month_date.month:02d}", "", "", "", "", "", ""], ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]]
        style_cmds = [
            ("SPAN", (0, 0), (-1, 0)),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("FONTNAME", (0, 0), (-1, -1), pdf_font),
            ("FONTSIZE", (0, 0), (-1, 0), 7),
            ("FONTSIZE", (0, 1), (-1, -1), 5),
            ("LEADING", (0, 1), (-1, -1), 6),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#39465e")),
            ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#dbe5f5")),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#8b95a5")),
            ("LEFTPADDING", (0, 0), (-1, -1), 1),
            ("RIGHTPADDING", (0, 0), (-1, -1), 1),
            ("TOPPADDING", (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]
        for week in calendar.Calendar(firstweekday=0).monthdatescalendar(month_date.year, month_date.month):
            cells = []
            for day in week:
                row_index = len(data)
                col_index = len(cells)
                if day.month != month_date.month:
                    cells.append("")
                    style_cmds.append(("BACKGROUND", (col_index, row_index), (col_index, row_index), colors.HexColor("#eef1f6")))
                    continue
                records = self.user.get("leave_records", {}).get(date_to_str(day), [])
                mark = self._short_day_mark(records, day, include_memo=True)
                cells.append(f"{day.day}\n{mark}" if mark else str(day.day))
                style_cmds.append(("BACKGROUND", (col_index, row_index), (col_index, row_index), self._pdf_day_color(day, records, colors)))
            data.append(cells)
        table = Table(data, colWidths=[28] * 7, rowHeights=[13, 11] + [22] * (len(data) - 2))
        table.setStyle(TableStyle(style_cmds))
        return table

    def _pdf_day_color(self, day: date, records: list[dict], colors):
        if any(r.get("status") == "실시완료" and r.get("mode") == "전일" for r in records):
            return colors.HexColor("#6fb879")
        if any(r.get("status") == "계획" and r.get("mode") == "전일" for r in records):
            return colors.HexColor("#c8f0c2")
        if any("반차" in r.get("mode", "") for r in records):
            return colors.HexColor("#ead7ff")
        if self.user.get("memos", {}).get(date_to_str(day)):
            return colors.HexColor("#dbeafe")
        if self.engine.is_holiday(day):
            return colors.HexColor("#fff0a8")
        if self.engine.is_weekend(day):
            return colors.HexColor("#f4f6fa")
        return colors.white

    def close(self) -> None:
        self.notifier.stop()
        self.save()
        self.root.destroy()


def main() -> None:
    root = Tk()
    root.title(APP_TITLE)
    try:
        auth = AuthManager()
    except SystemExit:
        root.destroy()
        return
    login = LoginWindow(root, auth)

    def open_app(user: dict) -> None:
        CalendarUI(root, auth, user)

    login.on_success = open_app
    root.mainloop()


if __name__ == "__main__":
    main()
