"""화면 위에서 마우스로 드래그해 기록할 영역을 지정하는 오버레이 (ShareX/캡처 도구 방식).

전체 화면(모든 모니터)을 반투명하게 어둡게 표시하고, 사용자가 드래그하는
영역만 밝게(원본 그대로) 보여주는 '스포트라이트' 방식으로 동작한다.
"""
import ctypes
import tkinter as tk

from PIL import Image, ImageEnhance, ImageGrab, ImageTk

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

BORDER_COLOR = "#FFEB3B"  # 밝은 노란색 (저시력 사용자도 잘 보이도록)
FILL_STIPPLE_COLOR = "#4CAF50"
INSTRUCTION = (
    "기록할 영역을 마우스로 드래그해서 선택하세요.\n"
    "Enter: 선택 완료   /   Esc: 취소 (창 전체 사용)"
)
CONFIRM_TEXT = "✓ 영역이 지정되었습니다!"


def _get_virtual_screen_rect() -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    x = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    y = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    w = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    h = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    return x, y, w, h


def select_region_from_screen(root: tk.Tk) -> tuple[int, int, int, int] | None:
    """전체 화면 위에 오버레이를 띄워 사각형 영역을 드래그로 선택하게 한다.

    반환값은 화면 절대 좌표 기준 (x1, y1, x2, y2). 취소(Esc) 시 None.
    """
    vx, vy, vw, vh = _get_virtual_screen_rect()

    try:
        screenshot = ImageGrab.grab(bbox=(vx, vy, vx + vw, vy + vh), all_screens=True)
    except TypeError:
        screenshot = ImageGrab.grab(bbox=(vx, vy, vx + vw, vy + vh))

    dimmed = ImageEnhance.Brightness(screenshot).enhance(0.35)

    win = tk.Toplevel(root)
    win.overrideredirect(True)
    win.geometry(f"{vw}x{vh}+{vx}+{vy}")
    win.attributes("-topmost", True)
    win.configure(cursor="cross", bg="black")
    win.focus_force()
    win.grab_set()

    canvas = tk.Canvas(win, width=vw, height=vh, highlightthickness=0, bg="black")
    canvas.pack()

    tk_dimmed = ImageTk.PhotoImage(dimmed)
    canvas.create_image(0, 0, anchor="nw", image=tk_dimmed)

    instruction_id = canvas.create_text(
        vw // 2,
        60,
        text=INSTRUCTION,
        fill="white",
        font=("맑은 고딕", 22, "bold"),
        justify="center",
    )
    canvas.create_text(
        vw // 2,
        62,
        text=INSTRUCTION,
        fill="black",
        font=("맑은 고딕", 22, "bold"),
        justify="center",
    )
    canvas.tag_raise(instruction_id)

    state = {
        "start": None,
        "rect_id": None,
        "bright_img_id": None,
        "bright_tk": None,
        "coord_text_id": None,
        "coords": None,
        "confirmed": False,
    }

    def norm_rect(x0, y0, x1, y1):
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))

    def on_press(event):
        state["start"] = (event.x, event.y)
        if state["rect_id"] is not None:
            canvas.delete(state["rect_id"])
            state["rect_id"] = None
        if state["bright_img_id"] is not None:
            canvas.delete(state["bright_img_id"])
            state["bright_img_id"] = None

    def on_drag(event):
        if state["start"] is None:
            return
        x0, y0 = state["start"]
        rx0, ry0, rx1, ry1 = norm_rect(x0, y0, event.x, event.y)
        if rx1 - rx0 < 1 or ry1 - ry0 < 1:
            return

        # 선택 영역만 원본(밝은) 화면으로 보여주는 '스포트라이트' 효과
        crop = screenshot.crop((rx0, ry0, rx1, ry1))
        state["bright_tk"] = ImageTk.PhotoImage(crop)
        if state["bright_img_id"] is not None:
            canvas.delete(state["bright_img_id"])
        state["bright_img_id"] = canvas.create_image(rx0, ry0, anchor="nw", image=state["bright_tk"])

        if state["rect_id"] is not None:
            canvas.delete(state["rect_id"])
        state["rect_id"] = canvas.create_rectangle(
            rx0, ry0, rx1, ry1,
            outline=BORDER_COLOR, width=4,
            fill=FILL_STIPPLE_COLOR, stipple="gray12",
        )

        info = f"{rx0},{ry0}   {rx1 - rx0} x {ry1 - ry0}"
        if state["coord_text_id"] is not None:
            canvas.delete(state["coord_text_id"])
        text_y = ry0 - 22 if ry0 - 22 > 10 else ry1 + 22
        state["coord_text_id"] = canvas.create_text(
            rx0 + (rx1 - rx0) // 2, text_y,
            text=info, fill=BORDER_COLOR, font=("맑은 고딕", 16, "bold"),
        )

        canvas.tag_raise(instruction_id)

    def finish_selection(_event=None):
        if state["confirmed"]:
            return
        if state["rect_id"] is None or state["start"] is None:
            return
        coords = canvas.coords(state["rect_id"])
        if len(coords) != 4:
            return
        x0, y0, x1, y1 = coords
        if x1 - x0 < 5 or y1 - y0 < 5:
            return
        state["coords"] = (int(x0) + vx, int(y0) + vy, int(x1) + vx, int(y1) + vy)
        state["confirmed"] = True

        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=tk_dimmed)
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        for dx, dy, color in ((2, 2, "black"), (0, 0, "#4CAF50")):
            canvas.create_text(
                cx + dx, cy + dy, text=CONFIRM_TEXT, fill=color,
                font=("맑은 고딕", 26, "bold"), justify="center",
            )
        win.after(700, win.destroy)

    def on_release(event):
        finish_selection()

    def on_cancel(_event=None):
        state["coords"] = None
        win.destroy()

    canvas.bind("<ButtonPress-1>", on_press)
    canvas.bind("<B1-Motion>", on_drag)
    canvas.bind("<ButtonRelease-1>", on_release)
    win.bind("<Return>", finish_selection)
    win.bind("<Escape>", on_cancel)

    win.wait_window()
    return state["coords"]
