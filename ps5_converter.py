"""
PS5 WebM -> MP4 (H.264) конвертер с аппаратным ускорением (NVIDIA NVENC / AMD AMF).

Зависимости:
    pip install customtkinter tkinterdnd2
Также нужен ffmpeg (и ffprobe) в PATH: https://www.gyan.dev/ffmpeg/builds/
"""

import os
import re
import shutil
import subprocess
import sys
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:  # перетаскивание станет недоступно, остальное работает
    DND_AVAILABLE = False

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Качество: подпись -> (CQ для NVENC, QP для AMF)
QUALITY = {
    "Высокое (больше размер)": (19, 19),
    "Баланс": (23, 23),
    "Компактное": (27, 27),
}

OUTPUT_FORMATS = ["MP4 (H.264)"]


# ----------------------------------------------------------------- ffmpeg ---
def find_tool(name):
    """Ищет ffmpeg/ffprobe: сначала рядом с программой, потом в PATH."""
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                           else os.path.abspath(__file__))
    exe = name + (".exe" if sys.platform == "win32" else "")
    local = os.path.join(base, exe)
    if os.path.isfile(local):
        return local
    return shutil.which(name)


def run_quiet(cmd, timeout=20):
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )


def encoder_works(ffmpeg, encoder):
    """Пробное кодирование: убеждаемся, что GPU и драйвер реально отвечают."""
    try:
        r = run_quiet([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=0.2:r=30",
            "-c:v", encoder, "-f", "null", "-",
        ])
        return r.returncode == 0
    except Exception:
        return False


def detect_encoder(ffmpeg):
    """Возвращает (кодек, описание). Порядок: NVIDIA -> AMD -> CPU (запасной)."""
    try:
        listing = run_quiet([ffmpeg, "-hide_banner", "-encoders"]).stdout
    except Exception:
        listing = ""
    for enc, name in (("h264_nvenc", "NVIDIA NVENC"), ("h264_amf", "AMD AMF")):
        if enc in listing and encoder_works(ffmpeg, enc):
            return enc, name
    return "libx264", "CPU (libx264) — GPU-кодек не найден"


def get_duration(ffprobe, path):
    try:
        r = run_quiet([
            ffprobe, "-v", "error", "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", path,
        ])
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def build_command(ffmpeg, encoder, src, dst, quality):
    cq, qp = QUALITY[quality]
    cmd = [ffmpeg, "-hide_banner", "-y", "-hwaccel", "auto", "-i", src,
           "-map", "0:v:0", "-map", "0:a?"]

    if encoder == "h264_nvenc":
        cmd += ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(cq), "-b:v", "0",
                "-profile:v", "high", "-spatial-aq", "1"]
    elif encoder == "h264_amf":
        cmd += ["-c:v", "h264_amf", "-usage", "transcoding", "-quality", "balanced",
                "-rc", "cqp", "-qp_i", str(qp), "-qp_p", str(qp + 2), "-qp_b", str(qp + 4),
                "-profile:v", "high"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cq),
                "-profile:v", "high"]

    cmd += ["-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            "-progress", "pipe:1", "-nostats", dst]
    return cmd


def unique_output(src):
    base = os.path.splitext(src)[0] + "_converted"
    out, i = base + ".mp4", 1
    while os.path.exists(out):
        i += 1
        out = f"{base}_{i}.mp4"
    return out


# -------------------------------------------------------------------- GUI ---
class BaseApp(ctk.CTk):
    pass


if DND_AVAILABLE:
    class BaseApp(ctk.CTk, TkinterDnD.DnDWrapper):  # noqa: F811
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)


class App(BaseApp):
    def __init__(self):
        super().__init__()
        self.title("PS5 WebM → MP4 Converter")
        self.geometry("620x560")
        self.minsize(560, 520)

        self.src_path = None
        self.proc = None
        self.cancelled = False
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")
        self.encoder, self.encoder_name = ("libx264", "—")

        self._build_ui()

        if not self.ffmpeg or not self.ffprobe:
            self.convert_btn.configure(state="disabled")
            self.status.configure(
                text="ffmpeg/ffprobe не найдены. Положите ffmpeg.exe и ffprobe.exe рядом с программой или добавьте в PATH.",
                text_color="#ff6b6b")
        else:
            self.status.configure(text="Определяю видеокарту…")
            threading.Thread(target=self._detect_gpu, daemon=True).start()

    # ---- интерфейс
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self, text="Конвертер видео с PS5",
                     font=ctk.CTkFont(size=24, weight="bold")).grid(
            row=0, column=0, pady=(22, 2))
        self.gpu_label = ctk.CTkLabel(self, text="Кодировщик: определяется…",
                                      text_color="gray70")
        self.gpu_label.grid(row=1, column=0, pady=(0, 14))

        # Зона перетаскивания
        self.drop_zone = ctk.CTkFrame(self, height=170, corner_radius=16,
                                      border_width=2, border_color="#3b82f6",
                                      fg_color=("gray85", "gray17"))
        self.drop_zone.grid(row=2, column=0, padx=28, sticky="ew")
        self.drop_zone.grid_propagate(False)
        self.drop_zone.grid_columnconfigure(0, weight=1)
        self.drop_zone.grid_rowconfigure(0, weight=1)

        hint = ("⬇  Перетащите WebM-файл сюда" if DND_AVAILABLE
                else "Перетаскивание недоступно\n(pip install tkinterdnd2)")
        self.drop_label = ctk.CTkLabel(self.drop_zone, text=hint,
                                       font=ctk.CTkFont(size=16),
                                       wraplength=480, justify="center")
        self.drop_label.grid(row=0, column=0, padx=10, pady=10)

        if DND_AVAILABLE:
            for w in (self.drop_zone, self.drop_label):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        self.pick_btn = ctk.CTkButton(self, text="Выбрать WebM с PS5", height=40,
                                      font=ctk.CTkFont(size=15), command=self._pick_file)
        self.pick_btn.grid(row=3, column=0, padx=28, pady=(14, 8), sticky="ew")

        # Настройки
        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=4, column=0, padx=28, pady=4, sticky="ew")
        opts.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(opts, text="Выходной формат", anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(opts, text="Качество", anchor="w").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.format_menu = ctk.CTkOptionMenu(opts, values=OUTPUT_FORMATS)
        self.format_menu.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.quality_menu = ctk.CTkOptionMenu(opts, values=list(QUALITY))
        self.quality_menu.set("Высокое (больше размер)")
        self.quality_menu.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(2, 0))

        # Кнопки действий
        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=5, column=0, padx=28, pady=(16, 8), sticky="ew")
        btns.grid_columnconfigure(0, weight=1)
        self.convert_btn = ctk.CTkButton(btns, text="Конвертировать", height=46,
                                         font=ctk.CTkFont(size=16, weight="bold"),
                                         fg_color="#16a34a", hover_color="#15803d",
                                         command=self._start)
        self.convert_btn.grid(row=0, column=0, sticky="ew")
        self.cancel_btn = ctk.CTkButton(btns, text="Отмена", height=46, width=110,
                                        fg_color="#dc2626", hover_color="#b91c1c",
                                        state="disabled", command=self._cancel)
        self.cancel_btn.grid(row=0, column=1, padx=(10, 0))

        # Прогресс
        self.progress = ctk.CTkProgressBar(self, height=14)
        self.progress.set(0)
        self.progress.grid(row=6, column=0, padx=28, pady=(8, 4), sticky="ew")
        self.status = ctk.CTkLabel(self, text="Выберите файл", text_color="gray70",
                                   wraplength=540)
        self.status.grid(row=7, column=0, padx=28, pady=(0, 16))

    # ---- GPU
    def _detect_gpu(self):
        enc, name = detect_encoder(self.ffmpeg)
        self.encoder, self.encoder_name = enc, name

        def apply():
            ok = enc != "libx264"
            self.gpu_label.configure(
                text=f"Кодировщик: {name}",
                text_color="#4ade80" if ok else "#fbbf24")
            self.status.configure(text="Выберите файл" if not self.src_path
                                  else os.path.basename(self.src_path))
        self.after(0, apply)

    # ---- выбор файла
    def _set_file(self, path):
        if not path.lower().endswith((".webm", ".mkv", ".mp4", ".mov")):
            messagebox.showwarning("Неверный файл", "Нужен видеофайл (WebM с PS5).")
            return
        self.src_path = path
        self.drop_label.configure(text=f"✔  {os.path.basename(path)}")
        self.progress.set(0)
        self.status.configure(text="Готово к конвертации", text_color="gray70")

    def _pick_file(self):
        p = filedialog.askopenfilename(
            title="Выберите WebM с PS5",
            filetypes=[("Видео", "*.webm *.mkv *.mp4 *.mov"), ("Все файлы", "*.*")])
        if p:
            self._set_file(p)

    def _on_drop(self, event):
        files = self.tk.splitlist(event.data)
        if files:
            self._set_file(files[0])

    # ---- конвертация
    def _start(self):
        if not self.src_path:
            messagebox.showinfo("Нет файла", "Сначала выберите или перетащите файл.")
            return
        self.cancelled = False
        self._set_busy(True)
        dst = unique_output(self.src_path)
        threading.Thread(target=self._worker, args=(self.src_path, dst), daemon=True).start()

    def _worker(self, src, dst):
        duration = get_duration(self.ffprobe, src)
        cmd = build_command(self.ffmpeg, self.encoder, src, dst, self.quality_menu.get())
        err_tail = []
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                creationflags=CREATE_NO_WINDOW)

            fps = speed = ""
            for line in self.proc.stdout:
                line = line.strip()
                err_tail = (err_tail + [line])[-12:]
                if line.startswith("fps="):
                    fps = line[4:]
                elif line.startswith("speed="):
                    speed = line[6:]
                elif line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                    m = re.match(r"out_time_(?:us|ms)=(\d+)", line)
                    if m and duration > 0:
                        frac = min(int(m.group(1)) / 1_000_000 / duration, 1.0)
                        self.after(0, self._update_progress, frac, fps, speed)
            self.proc.wait()
            code = self.proc.returncode
        except Exception as e:  # noqa: BLE001
            code, err_tail = -1, [str(e)]

        self.after(0, self._finish, code, dst, "\n".join(err_tail))

    def _update_progress(self, frac, fps, speed):
        self.progress.set(frac)
        extra = f" • {fps} fps" if fps else ""
        extra += f" • {speed}" if speed else ""
        self.status.configure(text=f"Конвертация: {frac * 100:.1f}%{extra}",
                              text_color="gray70")

    def _finish(self, code, dst, log):
        self._set_busy(False)
        if self.cancelled:
            if os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            self.progress.set(0)
            self.status.configure(text="Отменено", text_color="#fbbf24")
        elif code == 0:
            self.progress.set(1)
            self.status.configure(text=f"Готово: {dst}", text_color="#4ade80")
            if messagebox.askyesno("Готово", "Конвертация завершена.\nПоказать файл в папке?"):
                self._reveal(dst)
        else:
            self.status.configure(text="Ошибка конвертации", text_color="#ff6b6b")
            messagebox.showerror("Ошибка ffmpeg", log or f"Код выхода: {code}")

    def _cancel(self):
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _set_busy(self, busy):
        self.convert_btn.configure(state="disabled" if busy else "normal")
        self.pick_btn.configure(state="disabled" if busy else "normal")
        self.cancel_btn.configure(state="normal" if busy else "disabled")

    @staticmethod
    def _reveal(path):
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", path])
            else:
                subprocess.Popen(["xdg-open", os.path.dirname(path)])
        except Exception:
            pass


if __name__ == "__main__":
    App().mainloop()
