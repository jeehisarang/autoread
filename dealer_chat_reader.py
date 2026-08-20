"""딜러 채팅 파서 - 독립 실행 스크립트 (task.md "딜러 채팅 파서 안정화 + 메인 연결 준비").

기존 메인 파이프라인(main.py/recorder.py)과 완전히 분리된 별도 스크립트다.
테이블 창(히어로 닉네임용)과 딜러 채팅 창을 각각 골라서, 딜러 채팅을 실시간으로
캡처해 포지션/닉네임/행동/금액을 뽑고, 새 핸드/스트리트가 바뀔 때마다 기존
포맷([N게임 시작]/[스트리트]/"나 : ")으로 콘솔에 한 줄씩 출력한다. 메인
파이프라인에는 붙이지 않는다.

사용법:
  1) 실시간 모드 (기본, 대화형)
     python dealer_chat_reader.py
     -> ① 테이블 창을 고른다(히어로 자동 매칭용, 없으면 그냥 Enter로 건너뜀).
     -> ② 딜러 채팅 창을 고른다("딜러 채팅"이 제목에 있는 창이 맨 위에 뜬다).
     -> Ctrl+C로 종료. 작은 창/최소화된 창/카카오톡 창은 목록에서 자동으로 숨긴다.

  2) 창 목록만 확인
     python dealer_chat_reader.py --list-windows

  3) 창 제목(부분 일치)으로 자동 선택 + 한 번만 캡처하고 종료(대화형 입력 없음)
     python dealer_chat_reader.py --chat-window "딜러" --once
     python dealer_chat_reader.py --chat-window "딜러" --main-window "CoinPoker" --once
     --save-capture path.png 를 같이 주면 원본 캡처 이미지를 파일로도 저장한다.

  4) 정지 이미지 테스트 모드 (창 없이 캡처 파일로 파서만 확인할 때)
     python dealer_chat_reader.py --image path\\to\\dealer_chat.png [--hero-name 닉네임]
"""
import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# 창 제목은 다른 프로그램이 마음대로 붙인 임의의 유니코드 문자열이라, 콘솔
# 코드페이지(한국어 Windows는 보통 cp949)로 표현 안 되는 문자가 섞이면 print()가
# UnicodeEncodeError로 죽는 걸 실측으로 확인했다(예: Grok 창 제목의 특수기호).
# 표현 못 하는 문자만 대체하고 나머지는 정상 출력되게 한다.
try:
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass

from PIL import Image

from src import dealer_chat_parser, ocr_engine, window_capture
from src.dealer_chat_recorder import DealerChatRecorder
from src.table_layout import load_layout as load_table_layout
from src.table_layout import resolve_ratio_region

# 작은/최소화/카카오톡 창을 숨기는 필터(window_capture.filtered_windows)는
# main.py와 공유해서 쓴다 - 여기서 따로 만들지 않는다.


def _pick_window(prompt: str, allow_skip: bool = False, prioritize_substr: str | None = None) -> int | None:
    windows = window_capture.filtered_windows(prioritize_substr)
    print(f"\n{prompt}")
    for i, w in enumerate(windows):
        mark = "★ " if prioritize_substr and prioritize_substr.lower() in w.title.lower() else "  "
        print(f"  {mark}[{i}] {w.title}   ({w.width}x{w.height}) @ ({w.left}, {w.top})")
    if allow_skip:
        print("  [건너뛰기] 그냥 Enter")
    while True:
        raw = input("번호 입력: ").strip()
        if allow_skip and raw == "":
            return None
        if raw.isdigit() and 0 <= int(raw) < len(windows):
            return windows[int(raw)].hwnd
        print("잘못된 입력입니다. 다시 입력하세요.")


def _find_window_by_substring(substr: str) -> int | None:
    """제목에 substr(대소문자 무시)이 포함된 창을 찾는다(작은/최소화/카카오톡 창은
    후보에서 제외). 대화형 입력 없이 스크립트로 창을 고를 때 쓴다
    (--chat-window/--main-window). 여러 개 걸리면 첫 번째를 쓰고 후보 전체를 안내한다."""
    matches = [w for w in window_capture.filtered_windows() if substr.lower() in w.title.lower()]
    if not matches:
        print(f"[오류] 제목에 '{substr}'이(가) 포함된 창을 찾지 못했습니다. "
              f"--list-windows 로 창 목록을 확인하세요.")
        return None
    if len(matches) > 1:
        print(f"[안내] '{substr}'에 매칭되는 창이 {len(matches)}개 있습니다. 첫 번째를 사용합니다:")
        for w in matches:
            print(f"  - {w.title}   ({w.width}x{w.height}) @ ({w.left}, {w.top})")
    return matches[0].hwnd


def list_windows() -> None:
    windows = window_capture.filtered_windows()
    print(f"떠 있는 창 {len(windows)}개 (작은/최소화/카카오톡 창 제외):")
    for i, w in enumerate(windows):
        print(f"  [{i}] {w.title}   ({w.width}x{w.height}) @ ({w.left}, {w.top})")


def _read_hero_nickname(main_table_hwnd: int, ocr_lang: str) -> str:
    """테이블 창의 히어로 좌석(table_layout.json 기준) 영역에서 닉네임을 읽는다.
    기존 메인 파이프라인 로직은 호출하지 않고, 좌표 설정만 읽어서 재사용한다."""
    img = window_capture.capture_window(main_table_hwnd)
    if img is None:
        return ""
    table_layout = load_table_layout()
    hero_seat = table_layout.hero_seat()
    if hero_seat is None:
        return ""
    region = resolve_ratio_region(hero_seat.region, img.size)
    try:
        crop = img.crop(region)
    except Exception:
        return ""
    lines = ocr_engine.recognize_lines(crop, lang=ocr_lang)
    for line in lines:
        text = line.text.strip()
        # 스택 액수("24.96" 등) 같은 숫자 줄은 닉네임이 아니므로 건너뛴다.
        if text and not text.replace(",", "").replace(".", "").isdigit():
            return text
    return ""


def _select_windows(args):
    """테이블 창(히어로 매칭용, 선택)과 딜러 채팅 창(필수)을 고른다.
    task.md [B]: ① 테이블 창 -> ② 딜러 채팅 창 순서로 고른다."""
    main_hwnd = None
    if not args.hero_name:
        if args.main_window:
            main_hwnd = _find_window_by_substring(args.main_window)
        elif not args.chat_window:
            main_hwnd = _pick_window("① 테이블 창을 선택하세요 (히어로 자동 매칭용)", allow_skip=True)

    if args.chat_window:
        chat_hwnd = _find_window_by_substring(args.chat_window)
        if chat_hwnd is None:
            sys.exit(1)
    else:
        chat_hwnd = _pick_window("② 딜러 채팅 창을 선택하세요", prioritize_substr="딜러 채팅")

    return main_hwnd, chat_hwnd


def run_once(chat_hwnd, main_hwnd, args) -> None:
    """스냅샷 모드: 지금 보이는 내용을 한 번 파싱해서 그대로 찍는다(디버깅/좌표
    확인용). 실시간 상태(새 핸드 감지, 포지션 재시도 등)는 쓰지 않는다."""
    chat_img = window_capture.capture_window(chat_hwnd)
    if chat_img is None:
        print("[경고] 딜러 채팅 창을 캡처할 수 없습니다(최소화 여부 확인).")
        return

    if args.save_capture:
        try:
            chat_img.save(args.save_capture)
            print(f"[캡처 저장] {args.save_capture} ({chat_img.size[0]}x{chat_img.size[1]})")
        except Exception as e:
            print(f"[경고] 캡처 저장 실패: {e}")

    hero_name = args.hero_name or ""
    if main_hwnd is not None:
        hero_name = _read_hero_nickname(main_hwnd, args.lang) or hero_name

    layout = dealer_chat_parser.load_layout()
    parsed = dealer_chat_parser.parse_dealer_chat(chat_img, layout=layout, hero_name=hero_name, ocr_lang=args.lang)
    output = dealer_chat_parser.format_output(parsed, layout=layout)
    print(f"[딜러 채팅 파서] 히어로: {hero_name or '(미설정)'}\n")
    print(output or "(인식된 행동 없음)")


def run_live(chat_hwnd, main_hwnd, args) -> None:
    """실시간 루프: DealerChatRecorder로 새 핸드/스트리트/히어로 폴드 상태를
    이어가며, 새로 확정된 줄만 이어서 출력한다(task.md [D][E])."""
    recorder = DealerChatRecorder(layout=dealer_chat_parser.load_layout(), ocr_lang=args.lang)
    hero_name = args.hero_name or ""
    print("\n기록을 시작합니다. Ctrl+C로 종료.\n")

    try:
        while True:
            start = time.time()
            chat_img = window_capture.capture_window(chat_hwnd)
            if chat_img is None:
                print("[경고] 딜러 채팅 창을 캡처할 수 없습니다(최소화 여부 확인).")
                time.sleep(args.interval)
                continue

            if args.save_capture:
                try:
                    chat_img.save(args.save_capture)
                except Exception:
                    pass

            if main_hwnd is not None:
                nickname = _read_hero_nickname(main_hwnd, args.lang)
                if nickname:
                    hero_name = nickname

            for item in recorder.process_frame(chat_img, hero_name=hero_name):
                print(item.text)

            elapsed = time.time() - start
            time.sleep(max(0.0, args.interval - elapsed))
    except KeyboardInterrupt:
        print("\n종료합니다.")


def run_on_image(args) -> None:
    chat_img = Image.open(args.image).convert("RGB")
    layout = dealer_chat_parser.load_layout()
    parsed = dealer_chat_parser.parse_dealer_chat(
        chat_img, layout=layout, hero_name=args.hero_name or "", ocr_lang=args.lang
    )
    print(dealer_chat_parser.format_output(parsed, layout=layout) or "(인식된 행동 없음)")


def main():
    parser = argparse.ArgumentParser(description="딜러 채팅 파서 스크립트")
    parser.add_argument("--image", help="실시간 캡처 대신 이 이미지 파일 하나로 한 번만 파싱")
    parser.add_argument("--hero-name", help="히어로 닉네임을 직접 지정(테이블 창 자동 인식 생략)")
    parser.add_argument("--interval", type=float, default=1.5, help="캡처 주기(초, 기본 1.5)")
    parser.add_argument("--lang", default="ko", help="OCR 언어 태그 (기본 ko)")
    parser.add_argument("--list-windows", action="store_true", help="떠 있는 창 목록만 출력하고 종료")
    parser.add_argument("--chat-window", help="딜러 채팅 창 제목(부분 일치)로 자동 선택 - 대화형 입력 생략")
    parser.add_argument("--main-window", help="테이블 창 제목(부분 일치)로 자동 선택(히어로 매칭용)")
    parser.add_argument("--once", action="store_true", help="한 번만 캡처/출력하고 종료(스냅샷 모드, 루프 안 함)")
    parser.add_argument("--save-capture", help="캡처한 딜러 채팅 원본 이미지를 이 경로에 저장")
    args = parser.parse_args()

    if args.list_windows:
        list_windows()
        return

    if not ocr_engine.is_language_available(args.lang):
        print("[경고] Windows OCR 언어 팩이 설치되어 있지 않은 것 같습니다. "
              "설정 > 시간 및 언어 > 언어 및 지역에서 한국어 OCR을 설치하세요.")

    if args.image:
        run_on_image(args)
        return

    main_hwnd, chat_hwnd = _select_windows(args)
    if args.once:
        run_once(chat_hwnd, main_hwnd, args)
    else:
        run_live(chat_hwnd, main_hwnd, args)


if __name__ == "__main__":
    main()
