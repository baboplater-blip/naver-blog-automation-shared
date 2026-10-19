# config/win_console.py — 네이티브 Windows 콘솔 도구 출력 안전 디코딩
#
# schtasks·where·tasklist 등 네이티브 도구는 콘솔 **OEM 코드페이지**(한국어=cp949)로
# 출력한다. 계정 전환은 자식 GUI 를 PYTHONUTF8=1 로 재기동(한글 print 보호)하는데,
# 그 UTF-8 모드에서는 subprocess(text=True) 의 기본 디코딩이 utf-8 이 되어 cp949
# 출력을 못 읽고 리더 스레드가 UnicodeDecodeError(0xbf …)로 죽는다 — 계정 전환 후
# schtasks 상태 조회마다 항상 재현. OEM 으로 디코딩하고 errors='replace' 로 어떤
# locale/모드에서도 안전하게 만든다. (비 Windows 는 utf-8 폴백 — schtasks 자체가 없음)
import os

CONSOLE_ENCODING = "oem" if os.name == "nt" else "utf-8"
