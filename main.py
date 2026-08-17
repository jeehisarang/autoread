"""홀덤 게임 텍스트 기록 프로그램 - 실행 진입점.

사용법:
  1. [게임 창 선택]으로 홀덤 프로그램 창을 지정한다.
  2. [저장 파일 선택]으로 결과를 저장할 xlsx 파일을 지정한다.
  3. [기록 시작]을 누르면, 창 전체에서 Fold/Call/Raise/All-in 같은 행동 텍스트만
     내용 기준으로 골라서 포지션(UTG/HJ/CO/BTN/SB/BB)별로 자동 기록한다.
     내 자리(화면 하단 중앙)의 행동은 초록색으로 구분해서 표시한다.
     (좌석마다 위치가 달라도 상관없다. 영역 지정은 선택 사항이다.)
"""
import os
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import win32gui

from src import config as cfg_mod
from src import ocr_engine, window_capture
from src.recorder import Recorder
from src.region_select import select_region_from_screen
from src.xlsx_writer import XlsxLogger

FONT_NORMAL = ("맑은 고딕", 12)
FONT_BOLD = ("맑은 고딕", 13, "bold")
FONT_BIG = ("맑은 고딕", 15, "bold")
FONT_LOG = ("Consolas", 13)
FONT_LOG_HEADER = ("Consolas", 14, "bold")

HERO_COLOR = "#1B7A1B"  # 내 행동 강조색 (초록)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("홀덤 게임 텍스트 기록 프로그램")
        self.root.geometry("880x640")

        self.cfg = cfg_mod.load_config()

        self.selected_hwnd: int | None = None
        self.selected_title: str | None = self.cfg.get("window_title")
        self.region: tuple | None = tuple(self.cfg["region"]) if self.cfg.get("region") else None
        self.save_path: str | None = self.cfg.get("save_path")
        self.interval_sec = float(self.cfg.get("interval_sec", 0.8))
        self.ocr_lang = self.cfg.get("ocr_lang", "ko")

        self.recorder: Recorder | None = None
        self.xlsx_logger: XlsxLogger | None = None

        self._build_ui()
        self._check_ocr_language()

        if self.selected_title:
            hwnd = window_capture.find_window_by_title(self.selected_title)
            if hwnd:
                self.selected_hwnd = hwnd
        self._refresh_labels()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- UI 구성 ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 8}

        top = tk.Frame(self.root)
        top.pack(fill="x", **pad)

        row1 = tk.Frame(top)
        row1.pack(fill="x", pady=4)
        tk.Button(row1, text="① 게임 창 선택", font=FONT_BOLD, width=18, command=self.on_select_window).pack(side="left")
        self.lbl_window = tk.Label(row1, text="선택된 창: 없음", font=FONT_NORMAL, anchor="w")
        self.lbl_window.pack(side="left", padx=10)

        row2 = tk.Frame(top)
        row2.pack(fill="x", pady=4)
        tk.Button(row2, text="② 저장 파일 선택", font=FONT_BOLD, width=18, command=self.on_select_save_path).pack(side="left")
        self.lbl_path = tk.Label(row2, text="저장 파일: 없음", font=FONT_NORMAL, anchor="w")
        self.lbl_path.pack(side="left", padx=10)

        row3 = tk.Frame(top)
        row3.pack(fill="x", pady=4)
        tk.Button(
            row3, text="(선택) 특정 영역만 캡처", font=FONT_NORMAL, width=18, command=self.on_select_region
        ).pack(side="left")
        self.lbl_region = tk.Label(row3, text="영역: 창 전체 (기본값, 보통 그대로 두면 됩니다)", font=FONT_NORMAL, anchor="w")
        self.lbl_region.pack(side="left", padx=10)

        row4 = tk.Frame(top)
        row4.pack(fill="x", pady=10)
        self.btn_toggle = tk.Button(
            row4, text="▶ 기록 시작", font=FONT_BIG, width=16, bg="#2e7d32", fg="white", command=self.on_toggle
        )
        self.btn_toggle.pack(side="left")
        tk.Button(row4, text="다음 게임으로 넘기기", font=FONT_NORMAL, command=self.on_next_hand).pack(side="left", padx=10)

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
        self.lbl_window.config(text=f"선택된 창: {self.selected_title or '없음'}")
        if self.region:
            x1, y1, x2, y2 = self.region
            self.lbl_region.config(
                text=f"영역: 지정됨 (x={x1}, y={y1}, 크기 {x2 - x1} x {y2 - y1})"
            )
        else:
            self.lbl_region.config(text="영역: 창 전체 (기본값, 보통 그대로 두면 됩니다)")
        self.lbl_path.config(text=f"저장 파일: {self.save_path or '없음'}")

    # ---------- 이벤트 핸들러 ----------
    def on_select_window(self):
        windows = window_capture.enum_windows()
        picker = tk.Toplevel(self.root)
        picker.title("게임 창 선택")
        picker.geometry("560x420")
        picker.grab_set()

        tk.Label(picker, text="기록할 홀덤 게임 창을 선택하세요", font=FONT_BOLD).pack(pady=6)

        listbox = tk.Listbox(picker, font=FONT_NORMAL)
        listbox.pack(fill="both", expand=True, padx=10, pady=6)
        for w in windows:
            listbox.insert("end", w.title)

        def confirm(_event=None):
            sel = listbox.curselection()
            if not sel:
                return
            chosen = windows[sel[0]]
            self.selected_hwnd = chosen.hwnd
            self.selected_title = chosen.title
            self._refresh_labels()
            picker.destroy()

        listbox.bind("<Double-Button-1>", confirm)
        btn_row = tk.Frame(picker)
        btn_row.pack(pady=6)
        tk.Button(btn_row, text="선택", font=FONT_NORMAL, command=confirm).pack(side="left", padx=6)
        tk.Button(btn_row, text="취소", font=FONT_NORMAL, command=picker.destroy).pack(side="left", padx=6)

    def on_select_region(self):
        if not self.selected_hwnd:
            messagebox.showinfo("안내", "먼저 게임 창을 선택해주세요.")
            return

        try:
            win32gui.ShowWindow(self.selected_hwnd, 9)  # SW_RESTORE (최소화 상태면 복원)
            win32gui.SetForegroundWindow(self.selected_hwnd)
        except Exception:
            pass
        self.root.update()
        time.sleep(0.3)  # 창이 앞으로 나오고 다시 그려질 시간을 준다.

        abs_coords = select_region_from_screen(self.root)
        if abs_coords is None:
            self.region = None
            self._refresh_labels()
            return

        try:
            wl, wt, wr, wb = win32gui.GetWindowRect(self.selected_hwnd)
        except Exception:
            messagebox.showerror("오류", "창 위치를 확인할 수 없습니다. 창 전체를 사용합니다.")
            self.region = None
            self._refresh_labels()
            return

        win_w, win_h = wr - wl, wb - wt
        x1 = max(0, min(win_w, abs_coords[0] - wl))
        y1 = max(0, min(win_h, abs_coords[1] - wt))
        x2 = max(0, min(win_w, abs_coords[2] - wl))
        y2 = max(0, min(win_h, abs_coords[3] - wt))

        if x2 - x1 < 5 or y2 - y1 < 5:
            messagebox.showwarning(
                "안내", "선택한 영역이 게임 창 밖에 있습니다. 창 전체를 사용합니다."
            )
            self.region = None
        else:
            self.region = (x1, y1, x2, y2)
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

    def on_next_hand(self):
        if self.recorder and self.recorder.is_running():
            n = self.recorder.next_hand()
            self.set_status(f"수동으로 {n}번째 게임으로 넘어갔습니다.")
        else:
            messagebox.showinfo("안내", "기록이 시작된 상태에서만 사용할 수 있습니다.")

    def on_toggle(self):
        if self.recorder and self.recorder.is_running():
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self):
        if not self.selected_hwnd:
            messagebox.showinfo("안내", "먼저 게임 창을 선택해주세요.")
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

        self.recorder = Recorder(
            hwnd=self.selected_hwnd,
            region=self.region,
            xlsx_logger=self.xlsx_logger,
            interval_sec=self.interval_sec,
            ocr_lang=self.ocr_lang,
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
                "window_title": self.selected_title,
                "region": list(self.region) if self.region else None,
                "save_path": self.save_path,
                "interval_sec": self.interval_sec,
                "ocr_lang": self.ocr_lang,
            }
        )
        cfg_mod.save_config(self.cfg)

    def on_close(self):
        if self.recorder and self.recorder.is_running():
            self.recorder.stop()
        self._save_config()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        style.theme_use(style.theme_use())
    except Exception:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
