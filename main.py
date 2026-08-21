"""홀덤 게임 텍스트 기록 프로그램 - 실행 진입점.

사용법 (task.md "딜러 채팅 파서를 메인 파이프라인에 연결"):
  1. [① 테이블 창 선택]으로 홀덤 테이블 창을 지정한다 (히어로 닉네임 자동 인식용 -
     안 골라도 기록 자체는 되고, 그때는 모든 행동이 "포지션 ] 행동" 형식으로만 남는다).
  2. [② 딜러 채팅 창 선택]으로 딜러 채팅 창을 지정한다 (필수).
  3. [③ 저장 파일 선택]으로 결과를 저장할 xlsx 파일을 지정한다.
  4. [기록 시작]을 누르면, 딜러 채팅 창을 주기적으로 캡처해서 프리플랍/플랍/턴/
     리버 4개 컬럼을 OCR로 읽어 포지션/닉네임/행동/금액을 뽑고, 새 핸드가
     시작되면 [N게임 시작]을 찍는다. 테이블 창을 골랐으면 그 창 하단 중앙
     닉네임과 딜러 채팅 이름을 매칭해서 내 행동만 "나 : " 형식으로 구분한다.

  기존 아우라/밝기/딜러버튼 기반 인식(src/recorder.py의 Recorder)은 그대로
  남아있지만(삭제하지 않음), 이 화면에서는 더 이상 쓰지 않는다 - 딜러 채팅 기반
  인식(DealerChatBridge)으로 대체됐다.
"""
import os
import sys
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win32api
import win32event
import winerror

from src import config as cfg_mod
from src import ocr_engine, table_layout, window_capture
from src.dealer_chat_bridge import DealerChatBridge
from src.xlsx_writer import XlsxLogger

# task.md "표(그리드) 통합 UI": 상단 컨트롤 영역이 기록 표보다 화면을 더 많이
# 차지하던 걸 줄이려고 전반적으로 폰트를 한 단계씩 줄였다(기존 12/13/15 ->
# 10/11/12) - "정작 중요한 기록칸이 작고 글씨는 너무 크다"는 피드백 반영.
FONT_NORMAL = ("맑은 고딕", 10)
FONT_BOLD = ("맑은 고딕", 11, "bold")
FONT_BIG = ("맑은 고딕", 12, "bold")
FONT_GRID = ("Consolas", 11)
FONT_GRID_HEADER = ("맑은 고딕", 10, "bold")

HERO_COLOR = "#1B7A1B"  # 내 행동 강조색 (초록)

# task.md "표(그리드) 통합 UI": 메인 기록(딜러 채팅 순서 그대로)과 카드 상세
# (AI 인식 결과, 늦게 나올 수 있음)를 예전엔 별도 패널 두 개로 나눴는데, 이번엔
# 엑셀처럼 표 하나로 합친다 - 카드 인식 결과가 나중에 도착하면 새 줄을 추가하는
# 대신 그 정보가 속한 게임/스트리트의 "행"을 찾아 카드 칸만 채워 넣는다. 그러면
# 화면에 뜨는 시각은 지금처럼 늦어도 되면서(요구사항 그대로), 시각적으로는
# 정확히 같은 가로줄(같은 게임/스트리트)에 나란히 보인다 - 실제 저장되는
# xlsx에 hand_no/street가 모든 행에 이미 같이 저장되는 것과 동일한 대응관계를
# 화면에서도 그대로 재현한 것뿐이라, 새로운 타이밍/대기 로직은 전혀 없다.
GRID_ROW_TAGS = ("hand_header", "street_header", "result", "hero_action", "action")

# 프로그램을 여러 개 동시에 켜면 같은 xlsx 파일에 서로 저장을 시도하면서 충돌하고
# 행동이 누락되는 문제가 있었다. Windows 뮤텍스로 중복 실행 자체를 막는다 - 이 이름의
# 뮤텍스는 프로세스가 (정상 종료든 강제 종료든) 끝나면 OS가 자동으로 해제하므로,
# 이전 창을 안 닫고 잊어버려도 다음 실행 때 낡은 잠금이 남는 일은 없다.
_SINGLE_INSTANCE_MUTEX_NAME = "Local\\HoldemTextRecorder_SingleInstance"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("홀덤 게임 텍스트 기록 프로그램")
        # task.md "표(그리드) 통합 UI": 표 열(시간/게임/스트리트/기록/카드)이
        # 다 들어가도록 기존보다 넓히고, 압축된 컨트롤 덕분에 표 영역도 키운다.
        self.root.geometry("1180x720")

        self.cfg = cfg_mod.load_config()

        self.table_hwnd: int | None = None
        self.table_title: str | None = self.cfg.get("table_window_title")
        self.chat_hwnd: int | None = None
        self.chat_title: str | None = self.cfg.get("chat_window_title")
        self.save_path: str | None = self.cfg.get("save_path")
        # src/config.py DEFAULT_CONFIG와 같은 기본값(0.2)으로 맞춘다 - 예전엔 여기만
        # 0.8로 어긋나 있었는데, 폴링 간격이 너무 길면(특히 폴드가 빠르게 연달아
        # 일어나는 테이블에서) 딜러 채팅 창의 이전 폴링 행동 줄이 화면에서 완전히
        # 스크롤돼 사라져 "겹치는 줄이 하나도 없음" -> 새 핸드로 오판하는 문제가
        # 있었다(같은 핸드가 둘로 쪼개짐, 실측 확인: hand15/16이 홀카드까지 똑같은데
        # 분리됨). AI 호출은 별도 스레드풀에 던지기만 해서(대기 없음) 이 값을 줄여도
        # AI 지연과는 무관하다.
        self.interval_sec = float(self.cfg.get("interval_sec", 0.2))
        self.ocr_lang = self.cfg.get("ocr_lang", "ko")
        # task.md "히어로 닉네임 설정값 매칭": 좌석 OCR로 매번 재탐지하는 대신
        # 사용자가 미리 넣어둔 닉네임으로 히어로를 식별한다. 비워두면 기존
        # 좌석 OCR 자동 인식으로 폴백된다(DealerChatBridge 쪽에서 처리).
        self.hero_nickname_var = tk.StringVar(value=self.cfg.get("hero_nickname", ""))
        # task.md "히어로 닉네임 최근 목록 + 자동 저장": 최근 사용한 닉네임 최대 3개(최신순).
        self.recent_hero_names: list[str] = list(self.cfg.get("recent_hero_names") or [])
        # task.md "테이블 인원수(6인/7인) 선택 UI": 좌표/파싱 로직은 그대로 두고,
        # 기록 시작 전 선택한 값에 따라 table_layout(6max/7max) 파일만 바꿔 읽는다.
        self.table_type_var = tk.StringVar(value=self.cfg.get("table_type", "7max"))

        self.recorder: DealerChatBridge | None = None
        self.xlsx_logger: XlsxLogger | None = None

        # task.md "표(그리드) 통합 UI": 카드 인식 결과가 나중에 도착했을 때 어느
        # 행(hand_header 또는 street_header 행)에 채워 넣을지 찾기 위한 표.
        # hand_no는 세션마다 계속 증가만 하므로 리셋 없이 멤버십만 확인해도 된다
        # (다른 모듈들과 같은 관례).
        self._grid_row_for_hand: dict = {}  # hand_no -> Treeview item id
        self._grid_row_for_street: dict = {}  # (hand_no, street) -> Treeview item id

        self._build_ui()
        self._check_ocr_language()

        if self.table_title:
            hwnd = window_capture.find_window_by_title(self.table_title)
            if hwnd:
                self.table_hwnd = hwnd
        if self.chat_title:
            hwnd = window_capture.find_window_by_title(self.chat_title)
            if hwnd:
                self.chat_hwnd = hwnd
        self._refresh_labels()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI 구성 ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 8}

        top = tk.Frame(self.root)
        top.pack(fill="x", **pad)

        row1 = tk.Frame(top)
        row1.pack(fill="x", pady=4)
        tk.Button(row1, text="① 테이블 창 선택", font=FONT_BOLD, width=18, command=self.on_select_table_window).pack(side="left")
        self.lbl_table_window = tk.Label(row1, text="테이블 창: 없음", font=FONT_NORMAL, anchor="w")
        self.lbl_table_window.pack(side="left", padx=10)

        row1b = tk.Frame(top)
        row1b.pack(fill="x", pady=4)
        tk.Button(row1b, text="② 딜러 채팅 창 선택", font=FONT_BOLD, width=18, command=self.on_select_chat_window).pack(side="left")
        self.lbl_chat_window = tk.Label(row1b, text="딜러 채팅 창: 없음", font=FONT_NORMAL, anchor="w")
        self.lbl_chat_window.pack(side="left", padx=10)

        row2 = tk.Frame(top)
        row2.pack(fill="x", pady=4)
        tk.Button(row2, text="③ 저장 파일 선택", font=FONT_BOLD, width=18, command=self.on_select_save_path).pack(side="left")
        self.lbl_path = tk.Label(row2, text="저장 파일: 없음", font=FONT_NORMAL, anchor="w")
        self.lbl_path.pack(side="left", padx=10)

        # task.md "히어로 닉네임 설정값 매칭": 기록 시작 전에 입력 가능, 비워두면
        # 기존 좌석 OCR 자동 인식으로 폴백(DealerChatBridge._run 참고).
        row3 = tk.Frame(top)
        row3.pack(fill="x", pady=4)
        tk.Label(row3, text="히어로 닉네임", font=FONT_BOLD, width=18, anchor="w").pack(side="left")
        tk.Entry(row3, font=FONT_NORMAL, width=24, textvariable=self.hero_nickname_var).pack(side="left", padx=10)
        tk.Label(
            row3, text="(비워두면 테이블 하단 좌석에서 자동 인식)", font=FONT_NORMAL, fg="#666",
        ).pack(side="left")

        # task.md "히어로 닉네임 최근 목록 + 자동 저장": 최근 사용한 닉네임 최대 3개를
        # 버튼으로 보여주고, 클릭하면 입력란에 바로 채워 넣는다.
        row3b = tk.Frame(top)
        row3b.pack(fill="x", pady=2)
        tk.Label(row3b, text="최근 사용", font=FONT_NORMAL, width=18, anchor="w", fg="#666").pack(side="left")
        self.recent_hero_frame = tk.Frame(row3b)
        self.recent_hero_frame.pack(side="left", padx=10)
        self._refresh_recent_hero_buttons()

        # task.md "테이블 인원수(6인/7인) 선택 UI": 기록 시작 전에만 바꿀 수 있다
        # (start_recording에서 비활성화, stop_recording에서 다시 활성화).
        row3c = tk.Frame(top)
        row3c.pack(fill="x", pady=4)
        tk.Label(row3c, text="테이블 인원수", font=FONT_BOLD, width=18, anchor="w").pack(side="left")
        self.rad_6max = tk.Radiobutton(
            row3c, text="6인 테이블", font=FONT_NORMAL, variable=self.table_type_var, value="6max",
        )
        self.rad_6max.pack(side="left", padx=(10, 0))
        self.rad_7max = tk.Radiobutton(
            row3c, text="7인 테이블", font=FONT_NORMAL, variable=self.table_type_var, value="7max",
        )
        self.rad_7max.pack(side="left", padx=(10, 0))

        row4 = tk.Frame(top)
        row4.pack(fill="x", pady=10)
        self.btn_toggle = tk.Button(
            row4, text="▶ 기록 시작", font=FONT_BIG, width=16, bg="#2e7d32", fg="white", command=self.on_toggle
        )
        self.btn_toggle.pack(side="left")

        self.lbl_status = tk.Label(self.root, text="상태: 대기 중", font=FONT_BOLD, fg="#333", anchor="w")
        self.lbl_status.pack(fill="x", padx=10)

        # task.md "표(그리드) 통합 UI": 메인 기록과 카드 상세를 표 하나로 합친다
        # (아래 _build_grid 참고) - 카드 인식 결과는 늦게 나와도 같은 행에 채워
        # 넣힐 뿐, 새 타이밍/대기 로직은 없다(기존 즉시 출력 그대로).
        grid_frame = tk.Frame(self.root)
        grid_frame.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        self._build_grid(grid_frame)

    def _build_grid(self, parent) -> None:
        style = ttk.Style()
        style.configure("Log.Treeview", font=FONT_GRID, rowheight=22)
        style.configure("Log.Treeview.Heading", font=FONT_GRID_HEADER)

        container = tk.Frame(parent)
        container.pack(fill="both", expand=True)
        vscroll = ttk.Scrollbar(container, orient="vertical")
        vscroll.pack(side="right", fill="y")

        columns = ("time", "hand", "street", "main", "card", "participants")
        tree = ttk.Treeview(
            container, columns=columns, show="headings", style="Log.Treeview",
            yscrollcommand=vscroll.set,
        )
        vscroll.config(command=tree.yview)
        tree.pack(side="left", fill="both", expand=True)

        headings = {
            "time": ("시간", 68, False),
            "hand": ("게임", 44, False),
            "street": ("스트리트", 62, False),
            "main": ("기록 (딜러 채팅 순서 그대로)", 380, True),
            "card": ("카드 인식 결과 (AI, 늦게 나올 수 있음)", 260, True),
            # task.md "참가인원(추정) 표시": 안티 낸 사람 수 기반 추정치라 100%
            # 확정은 아니라는 걸 열 제목에도 남긴다.
            "participants": ("참가인원(추정)", 96, False),
        }
        for col, (label, width, stretch) in headings.items():
            tree.heading(col, text=label)
            tree.column(col, width=width, minwidth=40, stretch=stretch, anchor="w")

        tree.tag_configure("hand_header", foreground="#0D47A1", background="#E8F0FD")
        tree.tag_configure("street_header", foreground="#4A148C", background="#F3E9FB")
        tree.tag_configure("result", foreground="#B71C1C", background="#FDECEC")
        tree.tag_configure("hero_action", foreground=HERO_COLOR)
        tree.tag_configure("action", foreground="#000000")

        self.tree_log = tree

    def _clear_grid(self) -> None:
        self.tree_log.delete(*self.tree_log.get_children())
        self._grid_row_for_hand = {}
        self._grid_row_for_street = {}

    def _check_ocr_language(self):
        if not ocr_engine.is_language_available(self.ocr_lang):
            messagebox.showwarning(
                "한국어 OCR 언어 필요",
                "Windows에 한국어 OCR 언어 팩이 설치되어 있지 않은 것 같습니다.\n"
                "설정 > 시간 및 언어 > 언어 및 지역 에서 '한국어'를 추가하고,\n"
                "언어 옵션에서 '광학 문자 인식(OCR)' 기능을 설치해주세요.",
            )

    def _refresh_recent_hero_buttons(self):
        """최근 닉네임 버튼 목록을 self.recent_hero_names 기준으로 다시 그린다."""
        for child in self.recent_hero_frame.winfo_children():
            child.destroy()
        if not self.recent_hero_names:
            tk.Label(self.recent_hero_frame, text="(없음)", font=FONT_NORMAL, fg="#999").pack(side="left")
            return
        for name in self.recent_hero_names[:3]:
            tk.Button(
                self.recent_hero_frame, text=name, font=FONT_NORMAL,
                command=lambda n=name: self.hero_nickname_var.set(n),
            ).pack(side="left", padx=3)

    def _remember_hero_nickname(self, name: str):
        """닉네임을 최근 목록 맨 앞으로 올린다(중복 제거, 최대 3개 유지). 빈 값은 무시."""
        name = name.strip()
        if not name:
            return
        if name in self.recent_hero_names:
            self.recent_hero_names.remove(name)
        self.recent_hero_names.insert(0, name)
        self.recent_hero_names = self.recent_hero_names[:3]

    def _refresh_labels(self):
        self.lbl_table_window.config(text=f"테이블 창: {self.table_title or '없음 (히어로 매칭 생략됨)'}")
        self.lbl_chat_window.config(text=f"딜러 채팅 창: {self.chat_title or '없음'}")
        self.lbl_path.config(text=f"저장 파일: {self.save_path or '없음'}")

    # ---------- 이벤트 핸들러 ----------
    def _pick_window_dialog(self, dialog_title: str, list_label: str, prioritize_substr: str | None = None):
        """창 선택 대화상자 - 작은/최소화/카카오톡 창은 목록에서 뺀다
        (window_capture.filtered_windows, task.md [A] "이미 만든 2개 선택 UI/
        필터를 메인에 연결"). prioritize_substr을 주면 그 문자열이 제목에 포함된
        창을 목록 맨 위에 올린다. 반환: (hwnd, title) 또는 취소 시 (None, None)."""
        windows = window_capture.filtered_windows(prioritize_substr)
        picker = tk.Toplevel(self.root)
        picker.title(dialog_title)
        picker.geometry("560x420")
        picker.grab_set()

        tk.Label(picker, text=list_label, font=FONT_BOLD).pack(pady=6)

        listbox = tk.Listbox(picker, font=FONT_NORMAL)
        listbox.pack(fill="both", expand=True, padx=10, pady=6)
        # 같은 제목의 창이 여러 개 떠 있으면(예: CoinPoker 창을 두 개 켠 경우)
        # 제목만으로는 구분이 안 돼서, 창 크기와 화면 위치도 같이 보여준다.
        for w in windows:
            mark = "★ " if prioritize_substr and prioritize_substr.lower() in w.title.lower() else ""
            listbox.insert("end", f"{mark}{w.title}   ({w.width}x{w.height})  @ ({w.left}, {w.top})")

        result = {}

        def confirm(_event=None):
            sel = listbox.curselection()
            if not sel:
                return
            chosen = windows[sel[0]]
            result["hwnd"] = chosen.hwnd
            result["title"] = chosen.title
            picker.destroy()

        listbox.bind("<Double-Button-1>", confirm)
        btn_row = tk.Frame(picker)
        btn_row.pack(pady=6)
        tk.Button(btn_row, text="선택", font=FONT_NORMAL, command=confirm).pack(side="left", padx=6)
        tk.Button(btn_row, text="취소", font=FONT_NORMAL, command=picker.destroy).pack(side="left", padx=6)

        self.root.wait_window(picker)
        return result.get("hwnd"), result.get("title")

    def on_select_table_window(self):
        hwnd, title = self._pick_window_dialog(
            "① 테이블 창 선택", "홀덤 테이블 창을 선택하세요 (히어로 닉네임 자동 인식용)"
        )
        if hwnd is not None:
            self.table_hwnd, self.table_title = hwnd, title
            self._refresh_labels()

    def on_select_chat_window(self):
        hwnd, title = self._pick_window_dialog(
            "② 딜러 채팅 창 선택", "딜러 채팅 창을 선택하세요", prioritize_substr="딜러 채팅"
        )
        if hwnd is not None:
            self.chat_hwnd, self.chat_title = hwnd, title
            self._refresh_labels()

    def on_select_save_path(self):
        path = filedialog.asksaveasfilename(
            title="저장할 엑셀 파일 선택",
            defaultextension=".xlsx",
            filetypes=[("Excel 파일", "*.xlsx")],
            initialfile="holdem_log.xlsx",
        )
        if path:
            self.save_path = path
            self._refresh_labels()

    def on_toggle(self):
        if self.recorder and self.recorder.is_running():
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if not self.chat_hwnd:
            messagebox.showinfo("안내", "먼저 딜러 채팅 창을 선택해주세요.")
            return
        if not self.save_path:
            messagebox.showinfo("안내", "먼저 저장할 엑셀 파일을 선택해주세요.")
            return

        try:
            self.xlsx_logger = XlsxLogger(self.save_path)
        except Exception as e:
            messagebox.showerror("오류", f"엑셀 파일을 열 수 없습니다: {e}")
            return

        self._clear_grid()

        table_layout.set_active_table_type(self.table_type_var.get())

        self.recorder = DealerChatBridge(
            chat_hwnd=self.chat_hwnd,
            table_hwnd=self.table_hwnd,
            xlsx_logger=self.xlsx_logger,
            interval_sec=self.interval_sec,
            ocr_lang=self.ocr_lang,
            hero_nickname=self.hero_nickname_var.get().strip(),
            on_line=self._on_line,
            on_card_line=self._on_card_line,
            on_status=self.set_status,
            on_hero_missing=self.on_hero_missing,
        )
        self.recorder.start()
        self.btn_toggle.config(text="■ 기록 정지", bg="#c62828")
        self.rad_6max.config(state="disabled")
        self.rad_7max.config(state="disabled")
        self._save_config()

    def stop_recording(self):
        if self.recorder:
            self.recorder.stop()
        self.btn_toggle.config(text="▶ 기록 시작", bg="#2e7d32")
        self.rad_6max.config(state="normal")
        self.rad_7max.config(state="normal")
        self.set_status("상태: 대기 중")

    # ---------- 콜백 (백그라운드 스레드에서 호출되므로 after로 UI 스레드에 전달) ----------
    def _on_line(self, text, tag, hand_no, street):
        self.root.after(0, lambda: self._append_grid_row(text, tag, hand_no, street))

    def _on_card_line(self, text, tag, hand_no, street):
        # task.md "표(그리드) 통합 UI": 새 행을 추가하는 대신, 이 결과가 속한
        # 게임(hand_header)/스트리트(street_header)의 행을 찾아 "카드 인식 결과"
        # 칸만 채운다 - AI 응답이 늦게 와도 메인 기록 순서/행 배치엔 영향 없다.
        self.root.after(0, lambda: self._fill_card_cell(text, tag, hand_no, street))

    def _append_grid_row(self, text, tag, hand_no, street):
        now = datetime.now().strftime("%H:%M:%S")
        item = self.tree_log.insert(
            "", "end", values=(now, hand_no or "", street, text, "", ""), tags=(tag,),
        )
        self.tree_log.see(item)
        # task.md "표(그리드) 통합 UI": 나중에 카드 인식 결과가 도착했을 때 이
        # 행을 다시 찾아올 수 있도록 기억해둔다 - hand_header는 히어로 홀카드/
        # 참가인원(추정)이, street_header는 그 스트리트의 보드 확인 결과가
        # 여기로 채워진다.
        if tag == "hand_header" and hand_no:
            self._grid_row_for_hand[hand_no] = item
        elif tag == "street_header" and hand_no:
            self._grid_row_for_street[(hand_no, street)] = item

    def _fill_card_cell(self, text, tag, hand_no, street):
        # task.md "참가인원(추정) 표시": 별도 열("participants")에 채운다 -
        # 카드 인식 결과 열과 안 섞이게. hand_header 행(그 핸드의 "[N게임 시작]")
        # 을 그대로 재사용한다.
        column = "participants" if tag == "participants" else "card"
        if tag in ("hand_header", "participants"):
            item = self._grid_row_for_hand.get(hand_no)
        else:
            item = self._grid_row_for_street.get((hand_no, street))
        if item is None or not self.tree_log.exists(item):
            # 해당 행을 못 찾으면(예: 아주 드물게 순서가 어긋난 경우) 조용히
            # 새 행으로라도 남겨서 정보 자체가 유실되지는 않게 한다.
            self._append_grid_row(text, tag, hand_no, street)
            return
        self.tree_log.set(item, column, text)

    def set_status(self, text: str):
        self.root.after(0, lambda: self.lbl_status.config(text=f"상태: {text}"))

    def on_hero_missing(self):
        # task.md "히어로 탈락 감지 및 기록 자동 중지": 백그라운드 스레드(bridge._run)
        # 에서 호출되므로, 실제 정지 처리(버튼/라디오 상태 변경 포함)는 다른 콜백들과
        # 같은 관례대로 after로 UI 스레드에 넘긴다. bridge가 이미 자기 폴링 루프는
        # 스스로 멈췄지만(_stop_flag), UI 쪽 상태(버튼 텍스트, 인원수 선택 재활성화)는
        # stop_recording()을 직접 불러야 갱신된다.
        self.root.after(0, self._handle_hero_missing)

    def _handle_hero_missing(self):
        self.stop_recording()
        # stop_recording()이 마지막에 "상태: 대기 중"으로 덮어쓰므로, 그 뒤에
        # 경고 문구로 다시 덮어써서 최종적으로 사용자가 이 메시지를 보게 한다.
        self.set_status("경고: 히어로가 테이블에서 보이지 않습니다. 기록을 중지합니다.")

    def _save_config(self):
        hero_nickname = self.hero_nickname_var.get().strip()
        self._remember_hero_nickname(hero_nickname)
        self.cfg.update(
            {
                "table_window_title": self.table_title,
                "chat_window_title": self.chat_title,
                "save_path": self.save_path,
                "interval_sec": self.interval_sec,
                "ocr_lang": self.ocr_lang,
                "hero_nickname": hero_nickname,
                "recent_hero_names": self.recent_hero_names,
                "table_type": self.table_type_var.get(),
            }
        )
        cfg_mod.save_config(self.cfg)
        self._refresh_recent_hero_buttons()

    def on_close(self):
        if self.recorder and self.recorder.is_running():
            self.recorder.stop()
        self._save_config()
        self.root.destroy()


def main():
    # 뮤텍스를 함수 지역변수로만 들고 있어도 root.mainloop()가 끝날 때까지는 이
    # 스택 프레임이 살아있으므로 핸들이 유지된다(중간에 GC로 풀리지 않는다).
    mutex = win32event.CreateMutex(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
    already_running = win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS

    root = tk.Tk()
    if already_running:
        root.withdraw()
        messagebox.showwarning(
            "이미 실행 중",
            "홀덤 게임 텍스트 기록 프로그램이 이미 실행되고 있습니다.\n"
            "같은 프로그램을 여러 개 동시에 켜면 같은 저장 파일에 서로 저장하려다\n"
            "충돌해서 행동 기록이 누락될 수 있습니다.\n\n"
            "기존에 열려있는 창을 사용해주세요(작업표시줄에서 찾아보세요).",
        )
        root.destroy()
        return

    try:
        style = ttk.Style()
        style.theme_use(style.theme_use())
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
