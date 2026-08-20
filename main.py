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
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win32api
import win32event
import winerror

from src import config as cfg_mod
from src import ocr_engine, window_capture
from src.dealer_chat_bridge import DealerChatBridge
from src.xlsx_writer import XlsxLogger

FONT_NORMAL = ("맑은 고딕", 12)
FONT_BOLD = ("맑은 고딕", 13, "bold")
FONT_BIG = ("맑은 고딕", 15, "bold")
FONT_LOG = ("Consolas", 13)
FONT_LOG_HEADER = ("Consolas", 14, "bold")

HERO_COLOR = "#1B7A1B"  # 내 행동 강조색 (초록)

# 프로그램을 여러 개 동시에 켜면 같은 xlsx 파일에 서로 저장을 시도하면서 충돌하고
# 행동이 누락되는 문제가 있었다. Windows 뮤텍스로 중복 실행 자체를 막는다 - 이 이름의
# 뮤텍스는 프로세스가 (정상 종료든 강제 종료든) 끝나면 OS가 자동으로 해제하므로,
# 이전 창을 안 닫고 잊어버려도 다음 실행 때 낡은 잠금이 남는 일은 없다.
_SINGLE_INSTANCE_MUTEX_NAME = "Local\\HoldemTextRecorder_SingleInstance"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("홀덤 게임 텍스트 기록 프로그램")
        self.root.geometry("880x640")

        self.cfg = cfg_mod.load_config()

        self.table_hwnd: int | None = None
        self.table_title: str | None = self.cfg.get("table_window_title")
        self.chat_hwnd: int | None = None
        self.chat_title: str | None = self.cfg.get("chat_window_title")
        self.save_path: str | None = self.cfg.get("save_path")
        self.interval_sec = float(self.cfg.get("interval_sec", 0.8))
        self.ocr_lang = self.cfg.get("ocr_lang", "ko")
        # task.md "히어로 닉네임 설정값 매칭": 좌석 OCR로 매번 재탐지하는 대신
        # 사용자가 미리 넣어둔 닉네임으로 히어로를 식별한다. 비워두면 기존
        # 좌석 OCR 자동 인식으로 폴백된다(DealerChatBridge 쪽에서 처리).
        self.hero_nickname_var = tk.StringVar(value=self.cfg.get("hero_nickname", ""))

        self.recorder: DealerChatBridge | None = None
        self.xlsx_logger: XlsxLogger | None = None

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

        row4 = tk.Frame(top)
        row4.pack(fill="x", pady=10)
        self.btn_toggle = tk.Button(
            row4, text="▶ 기록 시작", font=FONT_BIG, width=16, bg="#2e7d32", fg="white", command=self.on_toggle
        )
        self.btn_toggle.pack(side="left")

        self.lbl_status = tk.Label(self.root, text="상태: 대기 중", font=FONT_BOLD, fg="#333", anchor="w")
        self.lbl_status.pack(fill="x", padx=10)

        log_frame = tk.Frame(self.root)
        log_frame.pack(fill="both", expand=True, padx=10, pady=10)
        tk.Label(log_frame, text="실시간 인식 기록 (아래로 갈수록 최신)", font=FONT_NORMAL, anchor="w").pack(fill="x")

        text_container = tk.Frame(log_frame)
        text_container.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(text_container)
        scrollbar.pack(side="right", fill="y")
        self.txt_log = tk.Text(text_container, font=FONT_LOG, yscrollcommand=scrollbar.set, wrap="word")
        self.txt_log.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.txt_log.yview)

        self.txt_log.tag_configure("hand_header", font=FONT_LOG_HEADER, foreground="#0D47A1")
        self.txt_log.tag_configure("street_header", font=FONT_LOG_HEADER, foreground="#4A148C")
        self.txt_log.tag_configure("result", font=FONT_LOG_HEADER, foreground="#B71C1C")
        self.txt_log.tag_configure("hero_action", font=FONT_LOG, foreground=HERO_COLOR)
        self.txt_log.tag_configure("action", font=FONT_LOG, foreground="#000000")

        self.txt_log.configure(state="disabled")

    def _check_ocr_language(self):
        if not ocr_engine.is_language_available(self.ocr_lang):
            messagebox.showwarning(
                "한국어 OCR 언어 필요",
                "Windows에 한국어 OCR 언어 팩이 설치되어 있지 않은 것 같습니다.\n"
                "설정 > 시간 및 언어 > 언어 및 지역 에서 '한국어'를 추가하고,\n"
                "언어 옵션에서 '광학 문자 인식(OCR)' 기능을 설치해주세요.",
            )

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

        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

        self.recorder = DealerChatBridge(
            chat_hwnd=self.chat_hwnd,
            table_hwnd=self.table_hwnd,
            xlsx_logger=self.xlsx_logger,
            interval_sec=self.interval_sec,
            ocr_lang=self.ocr_lang,
            hero_nickname=self.hero_nickname_var.get().strip(),
            on_line=self._on_line,
            on_status=self.set_status,
        )
        self.recorder.start()
        self.btn_toggle.config(text="■ 기록 정지", bg="#c62828")
        self._save_config()

    def stop_recording(self):
        if self.recorder:
            self.recorder.stop()
        self.btn_toggle.config(text="▶ 기록 시작", bg="#2e7d32")
        self.set_status("상태: 대기 중")

    # ---------- 콜백 (백그라운드 스레드에서 호출되므로 after로 UI 스레드에 전달) ----------
    def _on_line(self, text, tag):
        self.root.after(0, lambda: self._append_log(text, tag))

    def _append_log(self, text, tag):
        self.txt_log.configure(state="normal")
        if tag in ("hand_header", "street_header", "result"):
            self.txt_log.insert("end", "\n")
        self.txt_log.insert("end", text + "\n", tag)
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def set_status(self, text: str):
        self.root.after(0, lambda: self.lbl_status.config(text=f"상태: {text}"))

    def _save_config(self):
        self.cfg.update(
            {
                "table_window_title": self.table_title,
                "chat_window_title": self.chat_title,
                "save_path": self.save_path,
                "interval_sec": self.interval_sec,
                "ocr_lang": self.ocr_lang,
                "hero_nickname": self.hero_nickname_var.get().strip(),
            }
        )
        cfg_mod.save_config(self.cfg)

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
