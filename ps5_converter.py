"""
PS5 WebM -> MP4 конвертер с аппаратным ускорением (NVIDIA NVENC / AMD AMF).

 • SDR-ролики  -> H.264 8-bit, цвета BT.709 (корректно размечены).
 • HDR-ролики  -> HEVC 10-bit с сохранением HDR10 (PQ, BT.2020) — Davinci Resolve
                  видит правильные цвета. Либо (по желанию) H.264 SDR с тонмаппингом.

Зависимости: pip install customtkinter tkinterdnd2
Нужны ffmpeg.exe и ffprobe.exe рядом с программой или в PATH.
"""

import json
import os
import shutil
import subprocess
import sys
import threading
from tkinter import filedialog, messagebox

import customtkinter as ctk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
except ImportError:
    DND_AVAILABLE = False

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Качество -> значение CQ/CRF/QP (меньше = лучше и больше размер)
QUALITY = {
    "Высокое (больше размер)": 19,
    "Баланс": 23,
    "Компактное": 27,
}

OUTPUT_FORMATS = {
    "MP4 — авто (SDR: H.264, HDR: HEVC 10-bit)": "auto",
    "MP4 — всегда H.264 (HDR → SDR, тонмаппинг)": "h264",
}

# Тонмаппинг HDR (PQ, BT.2020) -> SDR (BT.709). Работает на CPU, на 4K60 медленно.
TONEMAP_VF = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)


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


def run_quiet(cmd, timeout=30):
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout, creationflags=CREATE_NO_WINDOW,
    )


def encoder_works(ffmpeg, encoder, pix_fmt="yuv420p"):
    """Пробное кодирование: убеждаемся, что GPU и драйвер реально отвечают."""
    try:
        r = run_quiet([
            ffmpeg, "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=0.2:r=30",
            "-pix_fmt", pix_fmt, "-c:v", encoder, "-f", "null", "-",
        ])
        return r.returncode == 0
    except Exception:
        return False


def detect_encoders(ffmpeg):
    """
    Возвращает dict: name, gpu (bool), h264, hevc (имена кодеков), zscale (bool).
    H.264 проверяется в 8 bit, HEVC — в 10 bit (p010le), как нужно для HDR.
    """
    try:
        enc_list = run_quiet([ffmpeg, "-hide_banner", "-encoders"]).stdout
        flt_list = run_quiet([ffmpeg, "-hide_banner", "-filters"]).stdout
    except Exception:
        enc_list = flt_list = ""

    result = {"name": "CPU (libx264/libx265) — GPU-кодек не найден", "gpu": False,
              "h264": "libx264", "hevc": "libx265", "zscale": " zscale " in flt_list}

    for vendor, h264, hevc in (("NVIDIA NVENC", "h264_nvenc", "hevc_nvenc"),
                               ("AMD AMF", "h264_amf", "hevc_amf")):
        h264_ok = h264 in enc_list and encoder_works(ffmpeg, h264)
        hevc_ok = hevc in enc_list and encoder_works(ffmpeg, hevc, "p010le")
        if h264_ok or hevc_ok:
            result.update(name=vendor, gpu=True,
                          h264=h264 if h264_ok else "libx264",
                          hevc=hevc if hevc_ok else "libx265")
            if not hevc_ok:
                result["name"] += " (HEVC 10-bit на CPU)"
            return result
    return result


def probe_video(ffprobe, path):
    info = {"duration": 0.0, "width": 0, "height": 0, "fps": 0.0, "pix_fmt": "",
            "transfer": "", "primaries": "", "hdr": False}
    try:
        r = run_quiet([
            ffprobe, "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height,r_frame_rate,pix_fmt,color_transfer,color_primaries"
            ":format=duration",
            "-of", "json", path])
        data = json.loads(r.stdout)
        st = data["streams"][0]
        info["width"] = int(st.get("width", 0))
        info["height"] = int(st.get("height", 0))
        info["pix_fmt"] = st.get("pix_fmt", "")
        info["transfer"] = st.get("color_transfer", "")
        info["primaries"] = st.get("color_primaries", "")
        num, den = st.get("r_frame_rate", "0/1").split("/")
        info["fps"] = float(num) / float(den) if float(den) else 0.0
        info["duration"] = float(data.get("format", {}).get("duration") or 0)
    except Exception:
        pass
    ten_bit = "10" in info["pix_fmt"] or "12" in info["pix_fmt"]
    info["hdr"] = (info["transfer"] in ("smpte2084", "arib-std-b67")
                   or (info["primaries"] == "bt2020" and ten_bit))
    return info


def describe(info):
    parts = []
    if info["width"]:
        parts.append(f"{info['width']}×{info['height']}")
    if info["fps"]:
        parts.append(f"{info['fps']:.0f} fps")
    if info["hdr"]:
        parts.append("HDR (HLG)" if info["transfer"] == "arib-std-b67"
                     else "HDR10 (PQ, BT.2020)")
    else:
        parts.append("SDR")
    return " • ".join(parts)


def plan_for(info, mode):
    """sdr | hdr_hevc | tonemap"""
    if not info["hdr"]:
        return "sdr"
    return "hdr_hevc" if mode == "auto" else "tonemap"


def build_command(ffmpeg, enc, info, src, dst, quality, mode):
    cq = QUALITY[quality]
    plan = plan_for(info, mode)
    codec = enc["hevc"] if plan == "hdr_hevc" else enc["h264"]

    cmd = [ffmpeg, "-hide_banner", "-y", "-hwaccel", "auto", "-i", src,
           "-map", "0:v:0", "-map", "0:a?"]
    if plan == "tonemap":
        cmd += ["-vf", TONEMAP_VF]

    if codec == "h264_nvenc":
        cmd += ["-c:v", codec, "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                "-cq", str(cq), "-b:v", "0", "-profile:v", "high", "-spatial-aq", "1"]
    elif codec == "hevc_nvenc":
        cmd += ["-c:v", codec, "-preset", "p5", "-tune", "hq", "-rc", "vbr",
                "-cq", str(cq), "-b:v", "0", "-profile:v", "main10",
                "-spatial-aq", "1", "-tag:v", "hvc1"]
    elif codec == "h264_amf":
        cmd += ["-c:v", codec, "-usage", "transcoding", "-quality", "balanced",
                "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq + 2),
                "-qp_b", str(cq + 4), "-profile:v", "high"]
    elif codec == "hevc_amf":
        cmd += ["-c:v", codec, "-usage", "transcoding", "-quality", "balanced",
                "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq + 2),
                "-tag:v", "hvc1"]
    elif codec == "libx265":
        trc = info["transfer"] if info["transfer"] in ("smpte2084", "arib-std-b67") else "smpte2084"
        cmd += ["-c:v", codec, "-preset", "fast", "-crf", str(cq), "-tag:v", "hvc1",
                "-x265-params",
                f"hdr10=1:repeat-headers=1:colorprim=bt2020:transfer={trc}:"
                "colormatrix=bt2020nc:log-level=error"]
    else:  # libx264
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(cq),
                "-profile:v", "high"]

    if plan == "hdr_hevc":
        pix = "yuv420p10le" if codec == "libx265" else "p010le"
        trc = info["transfer"] if info["transfer"] in ("smpte2084", "arib-std-b67") else "smpte2084"
        cmd += ["-pix_fmt", pix, "-color_primaries", "bt2020", "-color_trc", trc,
                "-colorspace", "bt2020nc", "-color_range", "tv"]
    else:
        cmd += ["-pix_fmt", "yuv420p", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]

    cmd += ["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
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
        self.geometry("640x620")
        self.minsize(580, 580)

        self.src_path = None
        self.info = None
        self.proc = None
        self.cancelled = False
        self.ffmpeg = find_tool("ffmpeg")
        self.ffprobe = find_tool("ffprobe")
        self.enc = {"name": "—", "gpu": False, "h264": "libx264",
                    "hevc": "libx265", "zscale": False}

        self._build_ui()

        if not self.ffmpeg or not self.ffprobe:
            self.convert_btn.configure(state="disabled")
            self.status.configure(
                text="ffmpeg/ffprobe не найдены. Положите ffmpeg.exe и ffprobe.exe "
                     "рядом с программой или добавьте в PATH.",
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
                                       wraplength=520, justify="center")
        self.drop_label.grid(row=0, column=0, padx=10, pady=10)

        if DND_AVAILABLE:
            for w in (self.drop_zone, self.drop_label):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        self.pick_btn = ctk.CTkButton(self, text="Выбрать WebM с PS5", height=40,
                                      font=ctk.CTkFont(size=15), command=self._pick_file)
        self.pick_btn.grid(row=3, column=0, padx=28, pady=(14, 8), sticky="ew")

        opts = ctk.CTkFrame(self, fg_color="transparent")
        opts.grid(row=4, column=0, padx=28, pady=4, sticky="ew")
        opts.grid_columnconfigure(0, weight=3)
        opts.grid_columnconfigure(1, weight=2)

        ctk.CTkLabel(opts, text="Выходной формат", anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(opts, text="Качество", anchor="w").grid(row=0, column=1, sticky="w", padx=(12, 0))
        self.format_menu = ctk.CTkOptionMenu(opts, values=list(OUTPUT_FORMATS),
                                             command=lambda _v: self._refresh_hint())
        self.format_menu.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self.quality_menu = ctk.CTkOptionMenu(opts, values=list(QUALITY))
        self.quality_menu.set("Высокое (больше размер)")
        self.quality_menu.grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=(2, 0))

        self.hint_label = ctk.CTkLabel(self, text="", text_color="gray70",
                                       wraplength=560, justify="left")
        self.hint_label.grid(row=5, column=0, padx=28, pady=(8, 0), sticky="w")

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.grid(row=6, column=0, padx=28, pady=(12, 8), sticky="ew")
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

        self.progress = ctk.CTkProgressBar(self, height=14)
        self.progress.set(0)
        self.progress.grid(row=7, column=0, padx=28, pady=(8, 4), sticky="ew")
        self.status = ctk.CTkLabel(self, text="Выберите файл", text_color="gray70",
                                   wraplength=560)
        self.status.grid(row=8, column=0, padx=28, pady=(0, 16))

    def _refresh_hint(self):
        """Подсказка о том, что именно будет сделано с выбранным файлом."""
        if not self.info:
            self.hint_label.configure(text="")
            return
        plan = plan_for(self.info, OUTPUT_FORMATS[self.format_menu.get()])
        texts = {
            "sdr": "SDR-видео → H.264, цвета BT.709. Без потерь в яркости и насыщенности.",
            "hdr_hevc": "HDR сохраняется: HEVC 10-bit, BT.2020 + PQ. "
                        "В DaVinci Resolve цвета и яркость будут как в оригинале.",
            "tonemap": "HDR → SDR с тонмаппингом (цвет сохраняется, без «серости»). "
                       "Выполняется на CPU, на 4K60 заметно медленнее.",
        }
        self.hint_label.configure(text=texts[plan])

    # ---- GPU
    def _detect_gpu(self):
        enc = detect_encoders(self.ffmpeg)
        self.enc = enc

        def apply():
            self.gpu_label.configure(
                text=f"Кодировщик: {enc['name']}",
                text_color="#4ade80" if enc["gpu"] else "#fbbf24")
            if not self.src_path:
                self.status.configure(text="Выберите файл", text_color="gray70")
        self.after(0, apply)

    # ---- выбор файла
    def _set_file(self, path):
        if not path.lower().endswith((".webm", ".mkv", ".mp4", ".mov")):
            messagebox.showwarning("Неверный файл", "Нужен видеофайл (WebM с PS5).")
            return
        self.src_path = path
        self.info = None
        self.drop_label.configure(text=f"✔  {os.path.basename(path)}\nАнализ файла…")
        self.progress.set(0)
        self.status.configure(text="Готово к конвертации", text_color="gray70")
        if self.ffprobe:
            threading.Thread(target=self._probe, args=(path,), daemon=True).start()

    def _probe(self, path):
        info = probe_video(self.ffprobe, path)

        def apply():
            if path != self.src_path:
                return
            self.info = info
            self.drop_label.configure(
                text=f"✔  {os.path.basename(path)}\n{describe(info)}")
            self._refresh_hint()
        self.after(0, apply)

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
        if self.info is None:
            self.info = probe_video(self.ffprobe, self.src_path)
        mode = OUTPUT_FORMATS[self.format_menu.get()]
        if plan_for(self.info, mode) == "tonemap" and not self.enc["zscale"]:
            messagebox.showerror(
                "Нет фильтра zscale",
                "В вашем ffmpeg нет zscale (libzimg), тонмаппинг невозможен.\n"
                "Выберите режим «авто» или возьмите полную сборку ffmpeg (gpl).")
            return
        self.cancelled = False
        self._set_busy(True)
        dst = unique_output(self.src_path)
        cmd = build_command(self.ffmpeg, self.enc, self.info, self.src_path, dst,
                            self.quality_menu.get(), mode)
        threading.Thread(target=self._worker, args=(cmd, dst, self.info["duration"]),
                         daemon=True).start()

    def _worker(self, cmd, dst, duration):
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
                elif line.startswith(("out_time_us=", "out_time_ms=")):
                    try:
                        us = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    if duration > 0:
                        frac = max(0.0, min(us / 1_000_000 / duration, 1.0))
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
