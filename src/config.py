"""설정 저장/불러오기 (config.json)"""
import json
import os

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")

DEFAULT_CONFIG = {
    "window_title": None,      # 마지막으로 선택한 창 제목 (참고용, hwnd는 재실행시 바뀌므로 제목으로 재탐색)
    "region": None,            # [x1, y1, x2, y2]  (창 내부 상대좌표, None이면 창 전체)
    "save_path": None,         # 저장할 xlsx 경로
    "interval_sec": 0.8,       # 캡처 주기(초)
    "ocr_lang": "ko",          # Windows OCR 언어 태그
}


def load_config() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            cfg = dict(DEFAULT_CONFIG)
            cfg.update(data)
            return cfg
        except Exception:
            pass
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
