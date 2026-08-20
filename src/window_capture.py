"""윈도우 목록 조회 및 특정 창 캡처.

특정 홀덤 프로그램에 종속되지 않도록, 화면에 떠 있는 모든 창을 나열해서
사용자가 직접 캡처할 창을 고르게 한다.

캡처는 순수 ctypes(user32/gdi32)로 구현한다. pywin32의 win32ui 모듈은
MFC 런타임(mfc140u.dll)이 시스템에 설치되어 있어야 동작하는데, 이 DLL은
일반 Windows에 기본으로 깔려있지 않은 경우가 많아 배포 시 문제가 될 수 있다.
"""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import win32gui
from PIL import Image, ImageGrab

PW_CLIENTONLY = 0x00000001
PW_RENDERFULLCONTENT = 0x00000002

gdi32 = ctypes.windll.gdi32


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.GetDIBits.argtypes = [
    wintypes.HDC,
    wintypes.HBITMAP,
    wintypes.UINT,
    wintypes.UINT,
    ctypes.c_void_p,
    ctypes.POINTER(_BITMAPINFO),
    wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int


@dataclass
class WindowInfo:
    hwnd: int
    title: str
    width: int
    height: int
    left: int
    top: int


def enum_windows() -> list[WindowInfo]:
    """제목이 있고 화면에 보이는 최상위 창 목록을 반환한다.

    제목이 같은 창이 여러 개 떠 있을 때(예: CoinPoker 창을 두 개 켠 경우)
    구분할 수 있도록 창 크기/위치도 같이 담는다(main.py 선택 목록에서 사용).
    """
    results: list[WindowInfo] = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return True
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left <= 0 or bottom - top <= 0:
            return True
        results.append(WindowInfo(hwnd=hwnd, title=title, width=right - left, height=bottom - top, left=left, top=top))
        return True

    win32gui.EnumWindows(_callback, None)
    return results


# task.md(딜러 채팅 창 선택 UI) "창 목록 필터": 작은 플레이스홀더 창(대부분
# 최소화된 앱이 win32에 남겨두는 껍데기 창)과 카카오톡 관련 창을 창 선택 목록에서
# 숨긴다. 실측 확인: 최소화된 창은 거의 다 크기 160x28에 좌표 (-32000,-32000)로
# 보고된다.
_JUNK_MIN_SIZE = (160, 28)
_JUNK_POS = (-32000, -32000)


def is_junk_window(w: WindowInfo) -> bool:
    if (w.width, w.height) == _JUNK_MIN_SIZE:
        return True
    if (w.left, w.top) == _JUNK_POS:
        return True
    title_lower = w.title.lower()
    if "kakaotalk" in title_lower or "카카오톡" in w.title:
        return True
    return False


def filtered_windows(prioritize_substr: str | None = None) -> list[WindowInfo]:
    """작은/최소화/카카오톡 창을 뺀 창 목록. prioritize_substr을 주면 제목에 그
    문자열이 포함된 창을 맨 앞으로 정렬한다(예: 딜러 채팅 창 고를 때 "딜러 채팅")."""
    windows = [w for w in enum_windows() if not is_junk_window(w)]
    if prioritize_substr:
        s = prioritize_substr.lower()
        windows.sort(key=lambda w: 0 if s in w.title.lower() else 1)
    return windows


def find_window_by_title(title: str) -> int | None:
    """저장된 제목과 완전히 일치하거나, 부분적으로 일치하는 창을 찾는다."""
    if not title:
        return None
    candidates = enum_windows()
    for w in candidates:
        if w.title == title:
            return w.hwnd
    for w in candidates:
        if title in w.title or w.title in title:
            return w.hwnd
    return None


def get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)


def _is_blank(img: Image.Image) -> bool:
    """캡처된 이미지가 사실상 빈 화면(검정/단색)인지 대략적으로 판단한다."""
    try:
        small = img.convert("L").resize((32, 32))
        pixels = list(small.getdata())
        return (max(pixels) - min(pixels)) < 4
    except Exception:
        return True


def _print_window(hwnd: int, width: int, height: int) -> Image.Image | None:
    # GetDC(클라이언트 DC)를 쓴다 - GetWindowDC(전체 창 DC)를 쓰면 제목표시줄까지
    # 포함돼서, table_layout.json의 비율 좌표(게임 화면만 있는 캡처 기준으로 실측)가
    # 제목표시줄 높이만큼 아래로 밀려 어긋난다(실측 확인: 코인포커 기준 제목표시줄
    # 28px, 창 전체 높이 대비 약 4%p - 좌석 박스처럼 세로 폭이 좁은 영역은 이 정도만
    # 어긋나도 통째로 빗나간다). PW_CLIENTONLY로 캡처 자체도 클라이언트 영역만 그리게 한다.
    hwnd_dc = win32gui.GetDC(hwnd)
    mem_dc = None
    bmp = None
    try:
        mem_dc = win32gui.CreateCompatibleDC(hwnd_dc)
        bmp = win32gui.CreateCompatibleBitmap(hwnd_dc, width, height)
        win32gui.SelectObject(mem_dc, bmp)

        ok = ctypes.windll.user32.PrintWindow(hwnd, mem_dc, PW_CLIENTONLY | PW_RENDERFULLCONTENT)

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = width
        bmi.bmiHeader.biHeight = -height  # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0

        buf = (ctypes.c_char * (width * height * 4))()
        lines = gdi32.GetDIBits(int(mem_dc), int(bmp), 0, height, buf, ctypes.byref(bmi), 0)
        if not ok or lines == 0:
            return None

        img = Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
        if _is_blank(img):
            return None
        return img
    except Exception:
        return None
    finally:
        try:
            if bmp is not None:
                win32gui.DeleteObject(bmp)
            if mem_dc is not None:
                win32gui.DeleteDC(mem_dc)
            if hwnd_dc is not None:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
        except Exception:
            pass


def capture_window(hwnd: int) -> Image.Image | None:
    """PrintWindow API로 창의 클라이언트 영역(제목표시줄/테두리 제외, 실제 게임
    화면)을 캡처한다. 실패 시 화면 캡처로 대체한다."""
    if not win32gui.IsWindow(hwnd):
        return None
    if win32gui.IsIconic(hwnd):
        # 최소화된 창은 캡처할 수 없다.
        return None

    cl, ct, cr, cb = win32gui.GetClientRect(hwnd)
    width, height = cr - cl, cb - ct
    if width <= 0 or height <= 0:
        return None

    img = _print_window(hwnd, width, height)

    if img is None:
        # PrintWindow가 실패하는 프로그램(일부 하드웨어 가속 렌더링 등)을 위한 대체 수단.
        # 창이 화면에 실제로 보이는 상태여야 한다. 클라이언트 영역의 화면 좌표를
        # 구해서 그 부분만 그랩한다(창 전체가 아니라 - 위 _print_window 주석 참고).
        try:
            sl, st = win32gui.ClientToScreen(hwnd, (0, 0))
            img = ImageGrab.grab(bbox=(sl, st, sl + width, st + height))
            if _is_blank(img):
                img = None
        except Exception:
            img = None

    return img
