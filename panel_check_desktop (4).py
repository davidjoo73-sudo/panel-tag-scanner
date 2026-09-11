"""
panel_check_desktop.py
------------------------------------------------------------
오프라인 Panel/TAG 인식 + BOM·TB LIST 대조 데스크톱 프로그램

- 클라우드 AI(Claude 등) 사용 없음 -> 사용 한도, 인터넷, 비용 걱정 없음
- 웹캠으로 촬영 -> 관심 영역을 마우스로 드래그해서 지정 -> Tesseract OCR로 문자 인식
- 인식 결과가 틀리면 그 자리에서 수정 -> "교정 기억(correction memory)"에 저장되어
  다음에 같은 오독이 생기면 자동으로 맞는 값을 제안 (완전한 AI 재학습은 아니지만,
  쓸수록 점점 덜 틀리는 실질적인 학습 효과)
- BOM/TB LIST(csv) 업로드 -> TAG 기준으로 대조 -> 일치/불일치 판정
- 세션 기록을 엑셀(xlsx) 또는 csv로 저장

필요 패키지 (PC에서 최초 1회 설치):
    pip install opencv-python pytesseract pillow openpyxl
    (Tesseract-OCR 프로그램 자체도 설치되어 있어야 합니다: https://github.com/UB-Mannheim/tesseract/wiki - Windows용)
"""

import os
import re
import csv
import json
import difflib
import datetime
import threading

# ============================================================
# 1) 코어 로직 (GUI 없이도 동작/테스트 가능한 순수 함수들)
# ============================================================

def normalize(s):
    """비교용 정규화: 공백 제거, 대문자 통일."""
    return re.sub(r'\s+', '', str(s or '')).strip().upper()


PANEL_COL_ALIASES = ['PANEL', 'PANELNO', 'PANEL_NO', '판넬', '판넬번호', '패널', '패널번호']
TAG_COL_ALIASES = ['TAG', '선번호', 'WIRENO', 'WIRE_NO', 'KEY', 'ID']


def load_reference_csv(path):
    """BOM/TB LIST csv를 읽어서 (rows, headers, key_col, panel_col)을 반환.
    PANEL류 컬럼을 먼저 찾아 제외한 뒤, TAG류 이름의 컬럼(없으면 첫 번째 비-PANEL 컬럼)을 키로 쓴다."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        rows = list(reader)

    panel_col = None
    for h in headers:
        hn = re.sub(r'[\s_]', '', h).upper()
        if any(a.replace('_', '') in hn for a in PANEL_COL_ALIASES):
            panel_col = h
            break

    key_col = None
    for h in headers:
        if h == panel_col:
            continue
        hn = re.sub(r'[\s_]', '', h).upper()
        if any(a.replace('_', '') in hn for a in TAG_COL_ALIASES):
            key_col = h
            break
    if key_col is None:
        # TAG류 이름을 못 찾으면, PANEL이 아닌 첫 번째 컬럼을 키로 사용
        for h in headers:
            if h != panel_col:
                key_col = h
                break

    return rows, headers, key_col, panel_col


def panel_values(rows, panel_col):
    if not panel_col:
        return []
    seen, out = set(), []
    for r in rows:
        v = (r.get(panel_col) or '').strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return sorted(out)


def match_against_list(tag, rows, key_col, panel_col=None, panel=None):
    """TAG로 리스트에서 행을 찾는다. panel이 주어지면 그 Panel 범위 안에서만 찾는다."""
    if not rows or not key_col:
        return None
    pool = rows
    if panel_col and panel:
        pool = [r for r in rows if normalize(r.get(panel_col)) == normalize(panel)]
    target = normalize(tag)
    if not target:
        return None
    for r in pool:
        if normalize(r.get(key_col)) == target:
            return r
    return None


FIELD_ALIASES = [
    ('maker', ['MAKER', 'MANUFACTURER', '제조사', '메이커']),
    ('model_no', ['MODEL', 'MODELNO', '모델']),
    ('serial_no', ['SERIAL', 'SERIALNO', '시리얼']),
    ('spec', ['SPEC', '사양', '규격', 'RATING']),
]


def row_field(row, field_key):
    """참조 리스트 행에서 maker/model_no/serial_no/spec에 해당하는 컬럼 값을 찾는다."""
    aliases = dict(FIELD_ALIASES).get(field_key, [])
    for c in row.keys():
        cn = re.sub(r'[\s_]', '', c).upper()
        if any(a.replace('_', '') in cn for a in aliases):
            return row.get(c)
    return None


# ---------- 교정 기억(correction memory): 완전한 AI 재학습이 아니라
#            "틀렸던 것 -> 맞는 것" 사전이 쌓이는 실질적 학습 장치 ----------

def load_memory(path):
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            try:
                return json.load(f)
            except Exception:
                return {}
    return {}


def save_memory(path, memory):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)


def suggest_from_memory(raw_text, memory, cutoff=0.72):
    """OCR 원문에 대해 교정 기억에서 힌트를 찾는다.
    정확히 같은 오독 기록이 있으면 그대로, 없으면 비슷한 것 중 가장 근접한 것을 제안."""
    key = normalize(raw_text)
    if not key:
        return None, 0.0
    if key in memory:
        return memory[key], 1.0
    candidates = list(memory.keys())
    best = difflib.get_close_matches(key, candidates, n=1, cutoff=cutoff)
    if best:
        ratio = difflib.SequenceMatcher(None, key, best[0]).ratio()
        return memory[best[0]], ratio
    return None, 0.0


def record_correction(memory, raw_text, corrected_text):
    """OCR이 틀렸고 사람이 고쳤을 때만 기억에 남긴다(원문==교정문이면 저장 안 함)."""
    key = normalize(raw_text)
    corr = normalize(corrected_text)
    if key and corr and key != corr:
        memory[key] = corr
        return True
    return False


# ---------- OCR (Tesseract, 완전 오프라인) ----------

def preprocess_for_ocr(cv_img):
    import cv2
    gray = cv2.cvtColor(cv_img, cv2.COLOR_BGR2GRAY)
    gray = cv2.resize(gray, None, fx=2.2, fy=2.2, interpolation=cv2.INTER_CUBIC)
    gray = cv2.bilateralFilter(gray, 5, 40, 40)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return th


def run_ocr(cv_img, lang='eng', psm=7):
    """psm=7: 한 줄짜리 텍스트로 취급(태그/라벨처럼 짧은 문자열에 적합)."""
    import pytesseract
    proc = preprocess_for_ocr(cv_img)
    config = f'--psm {psm}'
    text = pytesseract.image_to_string(proc, lang=lang, config=config)
    # OCR이 자주 헷갈리는 문자 정리는 여기서 최소한만: 좌우 공백/개행 제거
    return text.strip()


# ============================================================
# 2) 기록/내보내기
# ============================================================

def export_records(records, out_path):
    """records: list of dict. .xlsx면 openpyxl, 아니면 csv로 저장."""
    fieldnames = ['시간', 'PANEL', 'TAG', 'OCR원문', '확인/일치', '불일치 사유',
                  'MAKER', 'MODEL_NO', 'SERIAL_NO', 'SPEC']
    if out_path.lower().endswith('.xlsx'):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = 'BOM_Check'
        ws.append(fieldnames)
        for r in records:
            ws.append([r.get(k, '') for k in fieldnames])
        wb.save(out_path)
    else:
        with open(out_path, 'w', newline='', encoding='utf-8-sig') as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in records:
                w.writerow({k: r.get(k, '') for k in fieldnames})
    return out_path


def fetch_snapshot(url, timeout=3):
    """IP Webcam류 앱의 /shot.jpg 스냅샷 엔드포인트에서 정지 이미지를 한 장 가져온다.
    연속 스트림(/video)과 달리 매 요청이 독립적이라 불안정한 네트워크(Tailscale/모바일망)에서 훨씬 안정적이다."""
    import urllib.request
    import numpy as np
    import cv2
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        data = resp.read()
    arr = np.frombuffer(data, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    return img


# ============================================================
# 3) GUI (Tkinter + OpenCV 웹캠)
# ============================================================

def launch_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    import cv2
    from PIL import Image, ImageTk

    MEMORY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'correction_memory.json')

    class App:
        def __init__(self, root):
            self.root = root
            root.title('Panel/TB 오프라인 검수 프로그램 (Offline, No Cloud AI)')
            root.configure(bg='#14171a')
            root.geometry('1180x760')

            self.rows = []
            self.headers = []
            self.key_col = None
            self.panel_col = None
            self.selected_panel = None
            self.memory = load_memory(MEMORY_PATH)
            self.records = []

            self.cap = None
            self.camera_source = 0   # 0 = PC 기본 웹캠. 문자열(URL)을 넣으면 휴대폰 IP웹캠 등도 사용 가능.
            self.snapshot_url = None  # 설정되면 연속 스트림 대신 스냅샷 폴링 모드로 동작
            self.last_frame_ok_at = None
            self.frozen_frame = None   # 캡처된(정지) 프레임 (BGR ndarray)
            self.live_frame = None
            self.rect_start = None
            self.rect_end = None
            self.rect_id = None

            self._build_layout()
            self._start_camera()
            self._tick_camera()

        # ---------------- layout ----------------
        def _build_layout(self):
            bg = '#14171a'; panel_bg = '#1e2226'; fg = '#e8e6e1'; amber = '#f2a93b'
            style = ttk.Style()
            try:
                style.theme_use('clam')
            except Exception:
                pass
            style.configure('TButton', font=('Segoe UI', 10), padding=6)
            style.configure('Treeview', background='#1e2226', foreground=fg,
                             fieldbackground='#1e2226', rowheight=24)
            style.configure('Treeview.Heading', font=('Segoe UI', 9, 'bold'))

            top = tk.Frame(self.root, bg=bg)
            top.pack(fill='x', padx=10, pady=8)
            tk.Button(top, text='BOM/TB LIST 불러오기 (CSV)', command=self.on_load_list).pack(side='left')
            self.list_status = tk.Label(top, text='리스트 없음', fg='#8b9198', bg=bg)
            self.list_status.pack(side='left', padx=10)

            tk.Label(top, text='PANEL', fg=fg, bg=bg).pack(side='left', padx=(30, 4))
            self.panel_combo = ttk.Combobox(top, values=[], width=16, state='readonly')
            self.panel_combo.pack(side='left')
            self.panel_combo.bind('<<ComboboxSelected>>', self.on_panel_pick)

            self.mem_status = tk.Label(top, text='학습된 교정 항목: 0개', fg=amber, bg=bg)
            self.mem_status.pack(side='right')

            camf = tk.Frame(self.root, bg=bg)
            camf.pack(fill='x', padx=10, pady=(0, 6))
            tk.Label(camf, text='카메라 소스', fg=fg, bg=bg).pack(side='left')
            self.camera_source_var = tk.StringVar(value='0')
            tk.Entry(camf, textvariable=self.camera_source_var, width=42, bg='#24292e', fg=fg,
                     insertbackground=fg).pack(side='left', padx=6)
            tk.Button(camf, text='카메라 연결', command=self.on_connect_camera).pack(side='left')
            self.cam_status_label = tk.Label(camf, text='', fg='#8b9198', bg=bg)
            self.cam_status_label.pack(side='left', padx=8)
            tk.Label(camf, text='(0 = PC 기본 웹캠 / http://IP:8080/video = 연속스트림 / http://IP:8080/shot.jpg = 스냅샷모드, 불안정한 네트워크에 더 강함)',
                     fg='#8b9198', bg=bg, wraplength=520, justify='left').pack(side='left', padx=8)

            main = tk.Frame(self.root, bg=bg)
            main.pack(fill='both', expand=True, padx=10, pady=4)

            # ---- left: camera ----
            left = tk.Frame(main, bg=panel_bg)
            left.pack(side='left', fill='both', expand=True)
            self.canvas = tk.Canvas(left, width=640, height=480, bg='black', highlightthickness=0)
            self.canvas.pack(padx=8, pady=8)
            self.canvas.bind('<ButtonPress-1>', self.on_rect_start)
            self.canvas.bind('<B1-Motion>', self.on_rect_drag)
            self.canvas.bind('<ButtonRelease-1>', self.on_rect_end)

            cam_btns = tk.Frame(left, bg=panel_bg)
            cam_btns.pack(pady=(0, 10))
            tk.Button(cam_btns, text='📷 캡처(정지)', command=self.on_capture).pack(side='left', padx=4)
            tk.Button(cam_btns, text='🔄 다시 촬영', command=self.on_retake).pack(side='left', padx=4)
            tk.Button(cam_btns, text='🔎 선택영역 OCR 실행', command=self.on_run_ocr).pack(side='left', padx=4)
            tk.Label(left, text='※ 정지 화면 위에서 마우스로 드래그해 TAG/PANEL 글자 부분만 네모로 선택하세요',
                     fg='#8b9198', bg=panel_bg, wraplength=620, justify='left').pack(padx=8, pady=(0, 8))

            # ---- right: recognition/verify ----
            right = tk.Frame(main, bg=panel_bg, width=420)
            right.pack(side='left', fill='both', padx=(10, 0))

            tk.Label(right, text='인식 대상', fg=amber, bg=panel_bg, font=('Segoe UI', 10, 'bold')).pack(anchor='w', padx=10, pady=(10, 2))
            self.target_mode = tk.StringVar(value='TAG')
            modef = tk.Frame(right, bg=panel_bg)
            modef.pack(anchor='w', padx=10)
            tk.Radiobutton(modef, text='PANEL 인식', variable=self.target_mode, value='PANEL',
                           bg=panel_bg, fg=fg, selectcolor='#24292e').pack(side='left')
            tk.Radiobutton(modef, text='부품 TAG 인식', variable=self.target_mode, value='TAG',
                           bg=panel_bg, fg=fg, selectcolor='#24292e').pack(side='left')

            tk.Label(right, text='OCR 인식 결과 (틀리면 직접 수정)', fg=fg, bg=panel_bg).pack(anchor='w', padx=10, pady=(14, 2))
            self.ocr_var = tk.StringVar()
            self.ocr_entry = tk.Entry(right, textvariable=self.ocr_var, font=('Consolas', 16, 'bold'),
                                       bg='#24292e', fg=amber, insertbackground=fg)
            self.ocr_entry.pack(fill='x', padx=10)
            self.raw_ocr_text = ''  # 최초 OCR 원문 (교정 여부 판단용)

            self.hint_label = tk.Label(right, text='', fg='#5fd1a8', bg=panel_bg, wraplength=390, justify='left')
            self.hint_label.pack(anchor='w', padx=10, pady=(4, 0))

            tk.Button(right, text='이 값으로 확정 / 대조', command=self.on_confirm).pack(fill='x', padx=10, pady=10)

            self.verdict_label = tk.Label(right, text='', font=('Segoe UI', 11, 'bold'), bg=panel_bg, fg=fg,
                                           wraplength=390, justify='left')
            self.verdict_label.pack(anchor='w', padx=10)

            self.lookup_box = tk.Text(right, height=8, bg='#24292e', fg=fg, wrap='word', font=('Consolas', 9))
            self.lookup_box.pack(fill='x', padx=10, pady=8)
            self.lookup_box.configure(state='disabled')

            manualf = tk.Frame(right, bg=panel_bg)
            manualf.pack(fill='x', padx=10, pady=(0, 10))
            tk.Label(manualf, text='육안 대조 결과:', fg=fg, bg=panel_bg).pack(side='left')
            tk.Button(manualf, text='✓ 확인/일치', bg='#1f3d34', fg='#5fd1a8',
                      command=lambda: self.on_manual_verdict(True)).pack(side='left', padx=6)
            tk.Button(manualf, text='✕ 불일치', bg='#3d2220', fg='#e0665a',
                      command=lambda: self.on_manual_verdict(False)).pack(side='left', padx=6)
            self.mismatch_reason = tk.Entry(manualf, bg='#24292e', fg=fg, width=18)
            self.mismatch_reason.pack(side='left', padx=6)
            self.mismatch_reason.insert(0, '예: MAKER 상이')

            # ---- bottom: log table ----
            bottom = tk.Frame(self.root, bg=bg)
            bottom.pack(fill='both', expand=False, padx=10, pady=8)
            cols = ('시간', 'PANEL', 'TAG', '확인/일치', '불일치 사유')
            self.tree = ttk.Treeview(bottom, columns=cols, show='headings', height=7)
            for c in cols:
                self.tree.heading(c, text=c)
                self.tree.column(c, width=150 if c != '불일치 사유' else 220)
            self.tree.pack(fill='both', expand=True, side='left')

            botbtns = tk.Frame(bottom, bg=bg)
            botbtns.pack(side='left', padx=8)
            tk.Button(botbtns, text='엑셀로 내보내기 (.xlsx)', command=lambda: self.on_export('xlsx')).pack(fill='x', pady=2)
            tk.Button(botbtns, text='CSV로 내보내기', command=lambda: self.on_export('csv')).pack(fill='x', pady=2)

        # ---------------- camera ----------------
        def _start_camera(self):
            if self.cap is not None:
                try:
                    self.cap.release()
                except Exception:
                    pass
            self.cap = None
            self.snapshot_url = None
            source = self.camera_source
            if isinstance(source, str) and source.lower().rstrip('/').endswith('shot.jpg'):
                self.snapshot_url = source
                self.cam_status_label.configure(text='스냅샷 모드로 연결 대기 중…', fg='#f2a93b')
                return
            self.cap = cv2.VideoCapture(source)
            if not self.cap.isOpened():
                self.cam_status_label.configure(text='연결 실패', fg='#e0665a')
                messagebox.showwarning(
                    '카메라',
                    f'카메라를 열 수 없습니다: {source}\n'
                    '- PC 웹캠이면 0, 1, 2 순서로 바꿔보세요.\n'
                    '- 휴대폰 IP웹캠이면 주소가 맞는지, 같은 와이파이인지 확인해주세요.\n'
                    '- 연결이 자꾸 끊기면 주소 끝을 /video 대신 /shot.jpg 로 바꿔 스냅샷 모드를 써보세요.')
            else:
                self.cam_status_label.configure(text='연결됨(연속 스트림)', fg='#5fd1a8')

        def on_connect_camera(self):
            raw = self.camera_source_var.get().strip()
            if raw.isdigit():
                self.camera_source = int(raw)
            else:
                self.camera_source = raw
            self.frozen_frame = None
            self._start_camera()

        def _tick_camera(self):
            if self.frozen_frame is None:
                if self.snapshot_url:
                    if not getattr(self, 'snapshot_busy', False):
                        self.snapshot_busy = True
                        threading.Thread(target=self._fetch_snapshot_bg, daemon=True).start()
                    self.root.after(400, self._tick_camera)
                    return
                elif self.cap is not None and self.cap.isOpened():
                    ok, frame = self.cap.read()
                    if ok:
                        self.live_frame = frame
                        self._draw_frame(frame)
            self.root.after(33, self._tick_camera)

        def _fetch_snapshot_bg(self):
            try:
                img = fetch_snapshot(self.snapshot_url, timeout=3)
                if img is not None:
                    self.live_frame = img
                    self.last_frame_ok_at = datetime.datetime.now()

                    def ok_update():
                        self._draw_frame(img)
                        self.cam_status_label.configure(text='연결됨(스냅샷)', fg='#5fd1a8')
                    self.root.after(0, ok_update)
                else:
                    self.root.after(0, lambda: self.cam_status_label.configure(
                        text='스냅샷 응답을 이미지로 읽지 못함, 재시도 중…', fg='#e0665a'))
            except Exception as e:
                msg = f'연결 재시도 중… ({type(e).__name__})'
                self.root.after(0, lambda: self.cam_status_label.configure(text=msg, fg='#e0665a'))
            finally:
                self.snapshot_busy = False

        def _draw_frame(self, bgr_frame):
            rgb = cv2.cvtColor(bgr_frame, cv2.COLOR_BGR2RGB)
            img = Image.fromarray(rgb).resize((640, 480))
            self.photo = ImageTk.PhotoImage(img)
            self.canvas.delete('frame')
            self.canvas.create_image(0, 0, anchor='nw', image=self.photo, tags='frame')
            self.canvas.tag_lower('frame')

        def on_capture(self):
            if self.live_frame is not None:
                self.frozen_frame = self.live_frame.copy()
                self._draw_frame(self.frozen_frame)

        def on_retake(self):
            self.frozen_frame = None
            self.rect_start = self.rect_end = None
            if self.rect_id:
                self.canvas.delete(self.rect_id)
                self.rect_id = None

        # ---------------- rectangle select ----------------
        def on_rect_start(self, evt):
            if self.frozen_frame is None:
                return
            self.rect_start = (evt.x, evt.y)
            if self.rect_id:
                self.canvas.delete(self.rect_id)
            self.rect_id = self.canvas.create_rectangle(evt.x, evt.y, evt.x, evt.y, outline='#f2a93b', width=2)

        def on_rect_drag(self, evt):
            if self.rect_id and self.rect_start:
                self.canvas.coords(self.rect_id, self.rect_start[0], self.rect_start[1], evt.x, evt.y)

        def on_rect_end(self, evt):
            self.rect_end = (evt.x, evt.y)

        def _cropped_region(self):
            if self.frozen_frame is None or not self.rect_start or not self.rect_end:
                return None
            h, w = self.frozen_frame.shape[:2]
            # canvas는 640x480으로 리사이즈해서 보여주므로 원본 좌표로 환산
            sx, sy = w / 640.0, h / 480.0
            x1 = int(min(self.rect_start[0], self.rect_end[0]) * sx)
            x2 = int(max(self.rect_start[0], self.rect_end[0]) * sx)
            y1 = int(min(self.rect_start[1], self.rect_end[1]) * sy)
            y2 = int(max(self.rect_start[1], self.rect_end[1]) * sy)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(w, x2), min(h, y2)
            if x2 - x1 < 5 or y2 - y1 < 5:
                return self.frozen_frame  # 선택이 없으면 전체 프레임으로 OCR
            return self.frozen_frame[y1:y2, x1:x2]

        # ---------------- OCR ----------------
        def on_run_ocr(self):
            region = self._cropped_region()
            if region is None:
                messagebox.showinfo('안내', '먼저 "캡처(정지)"로 사진을 고정한 뒤 영역을 선택해주세요.')
                return
            raw = run_ocr(region, lang='eng', psm=7)
            self.raw_ocr_text = raw
            suggestion, score = suggest_from_memory(raw, self.memory)
            if suggestion and score >= 0.72:
                self.ocr_var.set(suggestion)
                self.hint_label.configure(
                    text=f'교정 기억에서 자동 보정됨: "{raw}" -> "{suggestion}" (유사도 {score:.2f})')
            else:
                self.ocr_var.set(raw)
                self.hint_label.configure(text=f'OCR 원문: "{raw}" (틀렸으면 위 칸에서 직접 고쳐주세요)')

        # ---------------- list ----------------
        def on_load_list(self):
            path = filedialog.askopenfilename(filetypes=[('CSV files', '*.csv'), ('All files', '*.*')])
            if not path:
                return
            try:
                rows, headers, key_col, panel_col = load_reference_csv(path)
            except Exception as e:
                messagebox.showerror('오류', f'리스트를 불러오지 못했습니다: {e}')
                return
            self.rows, self.headers, self.key_col, self.panel_col = rows, headers, key_col, panel_col
            self.list_status.configure(
                text=f'{len(rows)}개 항목 로드됨 (키: {key_col}' + (f', PANEL: {panel_col})' if panel_col else ')'))
            self.panel_combo['values'] = panel_values(rows, panel_col)

        def on_panel_pick(self, evt=None):
            self.selected_panel = self.panel_combo.get() or None

        # ---------------- confirm / match ----------------
        def on_confirm(self):
            final_text = self.ocr_var.get().strip()
            if not final_text:
                messagebox.showinfo('안내', '인식된 값이 없습니다. 먼저 OCR을 실행하거나 값을 입력하세요.')
                return

            learned = record_correction(self.memory, self.raw_ocr_text, final_text)
            if learned:
                save_memory(MEMORY_PATH, self.memory)
                self.mem_status.configure(text=f'학습된 교정 항목: {len(self.memory)}개')

            if self.target_mode.get() == 'PANEL':
                matched = None
                if self.panel_col:
                    for v in panel_values(self.rows, self.panel_col):
                        if normalize(v) == normalize(final_text):
                            matched = v
                            break
                self.selected_panel = matched or final_text
                if matched:
                    self.panel_combo.set(matched)
                self.verdict_label.configure(
                    text=f'PANEL: {self.selected_panel} 로 설정되었습니다. 이제 "부품 TAG 인식"으로 전환해서 촬영하세요.',
                    fg='#5fd1a8')
                self.target_mode.set('TAG')
                self._show_lookup(None)
                return

            row = match_against_list(final_text, self.rows, self.key_col, self.panel_col, self.selected_panel)
            self.current_tag = final_text
            self.current_row = row
            self._show_lookup(row)
            if row:
                self.verdict_label.configure(
                    text=f'TAG "{final_text}" 리스트에서 찾았습니다. 오른쪽 등록값과 실물 명판을 육안으로 비교한 뒤 아래에서 확인/불일치를 눌러주세요.',
                    fg='#f2a93b')
            else:
                self.verdict_label.configure(text=f'TAG "{final_text}" 이(가) 리스트에 없습니다 (미등록).', fg='#e0665a')

        def _show_lookup(self, row):
            self.lookup_box.configure(state='normal')
            self.lookup_box.delete('1.0', 'end')
            if row:
                for k, v in row.items():
                    self.lookup_box.insert('end', f'{k}: {v}\n')
            else:
                self.lookup_box.insert('end', '(리스트 등록 정보 없음)')
            self.lookup_box.configure(state='disabled')

        def on_manual_verdict(self, is_match):
            if not hasattr(self, 'current_tag'):
                messagebox.showinfo('안내', '먼저 TAG를 인식하고 "이 값으로 확정/대조"를 눌러주세요.')
                return
            row = getattr(self, 'current_row', None)
            confirm_text = '확인' if is_match else ''
            mismatch_text = '' if is_match else (self.mismatch_reason.get().strip() or '불일치')
            if row is None and is_match:
                mismatch_text = ''
            if row is None and not is_match:
                mismatch_text = mismatch_text or 'TAG 미등록'

            rec = {
                '시간': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'PANEL': self.selected_panel or '',
                'TAG': self.current_tag,
                'OCR원문': self.raw_ocr_text,
                '확인/일치': confirm_text,
                '불일치 사유': mismatch_text,
                'MAKER': row_field(row, 'maker') if row else '',
                'MODEL_NO': row_field(row, 'model_no') if row else '',
                'SERIAL_NO': row_field(row, 'serial_no') if row else '',
                'SPEC': row_field(row, 'spec') if row else '',
            }
            self.records.append(rec)
            self.tree.insert('', 0, values=(rec['시간'], rec['PANEL'], rec['TAG'], rec['확인/일치'], rec['불일치 사유']))

        # ---------------- export ----------------
        def on_export(self, kind):
            if not self.records:
                messagebox.showinfo('안내', '내보낼 기록이 없습니다.')
                return
            default_name = f'bom_check_{datetime.datetime.now().strftime("%Y%m%d_%H%M%S")}.{kind}'
            path = filedialog.asksaveasfilename(defaultextension=f'.{kind}', initialfile=default_name)
            if not path:
                return
            export_records(self.records, path)
            messagebox.showinfo('완료', f'저장되었습니다: {path}')

    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    launch_gui()
