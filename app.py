import os
import sys
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QIcon, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QFileDialog, QComboBox, QLineEdit, QProgressBar,
    QListWidget, QListWidgetItem, QMessageBox, QCheckBox, QSpinBox, QGroupBox,
    QFormLayout, QSlider
)

SUPPORTED_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
OUT_FORMATS = ["PNG", "JPG", "WEBP"]

def is_image_file(p: Path) -> bool:
    return p.is_file() and p.suffix.lower() in SUPPORTED_EXTS

def collect_images_from_path(p: Path) -> List[Path]:
    if p.is_file():
        return [p] if is_image_file(p) else []
    if p.is_dir():
        out: List[Path] = []
        for root, _, files in os.walk(p):
            for f in files:
                fp = Path(root) / f
                if is_image_file(fp):
                    out.append(fp)
        return out
    return []

def safe_mkdir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def unique_path(path: Path) -> Path:
    """If file exists, create 'name (1).ext' etc."""
    if not path.exists():
        return path
    i = 1
    while True:
        candidate = path.with_stem(f"{path.stem} ({i})")
        if not candidate.exists():
            return candidate
        i += 1

def compute_new_size(
    w: int, h: int,
    mode: str,
    keep_aspect: bool,
    width: Optional[int],
    height: Optional[int],
    percent: Optional[int]
) -> Tuple[int, int]:
    """Resize helper."""
    if mode == "none":
        return w, h
    if mode == "percent" and percent:
        nw = max(1, int(round(w * (percent / 100.0))))
        nh = max(1, int(round(h * (percent / 100.0))))
        return nw, nh
    if not width and not height:
        return w, h
    if keep_aspect:
        if width and not height:
            r = width / float(w)
            return width, max(1, int(round(h * r)))
        if height and not width:
            r = height / float(h)
            return max(1, int(round(w * r))), height
        if width and height:
            rw = width / float(w)
            rh = height / float(h)
            r = min(rw, rh)
            return max(1, int(round(w * r))), max(1, int(round(h * r)))
    else:
        return (width or w), (height or h)
    return w, h

def save_image(img: Image.Image, out_path: Path, fmt: str, quality: int) -> None:
    fmt_upper = fmt.upper()
    if fmt_upper in ("JPG", "JPEG"):
        if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        img.save(out_path, format="JPEG", quality=quality, optimize=True)
        return
    if fmt_upper == "PNG":
        img.save(out_path, format="PNG", optimize=True)
        return
    if fmt_upper == "WEBP":
        img.save(out_path, format="WEBP", quality=quality, method=6)
        return
    img.save(out_path)

class ConvertWorker(QThread):
    progress = Signal(int, int)       # done, total
    finished_ok = Signal(Path)        # output folder
    failed = Signal(str)

    def __init__(
        self,
        files: List[Path],
        out_dir: Path,
        out_fmt: str,
        resize_mode: str,
        keep_aspect: bool,
        width: Optional[int],
        height: Optional[int],
        percent: Optional[int],
        quality: int
    ):
        super().__init__()
        self.files = files
        self.out_dir = out_dir
        self.out_fmt = out_fmt
        self.resize_mode = resize_mode
        self.keep_aspect = keep_aspect
        self.width = width
        self.height = height
        self.percent = percent
        self.quality = quality

    def run(self) -> None:
        try:
            safe_mkdir(self.out_dir)
            total = len(self.files)
            done = 0
            for fp in self.files:
                try:
                    with Image.open(fp) as im:
                        im.load()
                        w, h = im.size
                        nw, nh = compute_new_size(
                            w, h,
                            self.resize_mode,
                            self.keep_aspect,
                            self.width,
                            self.height,
                            self.percent
                        )
                        if (nw, nh) != (w, h):
                            im = im.resize((nw, nh), Image.LANCZOS)
                        ext = "jpg" if self.out_fmt == "JPG" else self.out_fmt.lower()
                        out_name = fp.stem + "." + ext
                        out_path = unique_path(self.out_dir / out_name)
                        save_image(im, out_path, self.out_fmt, self.quality)
                except Exception:
                    pass
                done += 1
                self.progress.emit(done, total)
            self.finished_ok.emit(self.out_dir)
        except Exception as e:
            self.failed.emit(str(e))

class DropArea(QLabel):
    files_dropped = Signal(list)
    def __init__(self):
        super().__init__()
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setText("⬇ Drag & Drop Images or Folders Here\n(Works offline)")
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #888;
                border-radius: 12px;
                padding: 24px;
                font-size: 14px;
            }
        """)
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
    def dropEvent(self, event):
        urls = event.mimeData().urls()
        paths = [Path(u.toLocalFile()) for u in urls]
        self.files_dropped.emit(paths)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("BatchPix — Offline Image Converter")
        self.resize(820, 560)

        icon_path = Path(__file__).parent / "assets" / "icon.ico"
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))

        self.files: List[Path] = []
        self.worker: Optional[ConvertWorker] = None
        self.last_output_dir: Optional[Path] = None

        roadmap_action = QAction("Roadmap", self)
        roadmap_action.triggered.connect(self.show_roadmap)
        self.menuBar().addAction(roadmap_action)

        root = QWidget()
        self.setCentralWidget(root)
        main = QVBoxLayout(root)

        header = QLabel("BatchPix")
        header.setStyleSheet("font-size: 22px; font-weight: 700;")
        sub = QLabel("Offline • Drag & drop • Batch convert")
        sub.setStyleSheet("font-size: 12px; color: #666;")
        main.addWidget(header)
        main.addWidget(sub)

        self.drop = DropArea()
        self.drop.files_dropped.connect(self.on_drop)
        main.addWidget(self.drop)

        mid = QHBoxLayout()
        main.addLayout(mid)

        left_box = QGroupBox("Files")
        left_layout = QVBoxLayout(left_box)
        self.listw = QListWidget()
        left_layout.addWidget(self.listw)

        row_btns = QHBoxLayout()
        self.btn_add = QPushButton("Add Files…")
        self.btn_add.clicked.connect(self.add_files_dialog)
        self.btn_add_folder = QPushButton("Add Folder…")
        self.btn_add_folder.clicked.connect(self.add_folder_dialog)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self.clear_files)
        row_btns.addWidget(self.btn_add)
        row_btns.addWidget(self.btn_add_folder)
        row_btns.addWidget(self.btn_clear)
        left_layout.addLayout(row_btns)

        mid.addWidget(left_box, 2)

        right_box = QGroupBox("Output Settings")
        form = QFormLayout(right_box)

        self.combo_fmt = QComboBox()
        self.combo_fmt.addItems(OUT_FORMATS)
        self.combo_fmt.currentIndexChanged.connect(self.update_quality_state)
        form.addRow("Format:", self.combo_fmt)

        self.combo_resize = QComboBox()
        self.combo_resize.addItems(["No resize", "Width/Height", "Percent"])
        self.combo_resize.currentIndexChanged.connect(self.update_resize_ui)
        form.addRow("Resize:", self.combo_resize)

        self.chk_aspect = QCheckBox("Keep aspect ratio")
        self.chk_aspect.setChecked(True)
        form.addRow("", self.chk_aspect)

        self.spin_w = QSpinBox()
        self.spin_w.setRange(1, 20000)
        self.spin_w.setValue(1920)
        self.spin_h = QSpinBox()
        self.spin_h.setRange(1, 20000)
        self.spin_h.setValue(1080)

        wh_row = QHBoxLayout()
        wh_row.addWidget(QLabel("W"))
        wh_row.addWidget(self.spin_w)
        wh_row.addSpacing(8)
        wh_row.addWidget(QLabel("H"))
        wh_row.addWidget(self.spin_h)
        self.wh_container = QWidget()
        self.wh_container.setLayout(wh_row)
        form.addRow("Size:", self.wh_container)

        self.spin_percent = QSpinBox()
        self.spin_percent.setRange(1, 500)
        self.spin_percent.setValue(100)
        form.addRow("Percent:", self.spin_percent)

        self.slider_q = QSlider(Qt.Horizontal)
        self.slider_q.setRange(1, 100)
        self.slider_q.setValue(85)
        self.lbl_q = QLabel("85")
        self.slider_q.valueChanged.connect(lambda v: self.lbl_q.setText(str(v)))
        q_row = QHBoxLayout()
        q_row.addWidget(self.slider_q)
        q_row.addWidget(self.lbl_q)
        q_container = QWidget()
        q_container.setLayout(q_row)
        form.addRow("Quality:", q_container)

        out_row = QHBoxLayout()
        self.edit_out = QLineEdit()
        self.edit_out.setPlaceholderText("Choose output folder…")
        self.btn_out = QPushButton("Browse")
        self.btn_out.clicked.connect(self.pick_output_folder)
        out_row.addWidget(self.edit_out)
        out_row.addWidget(self.btn_out)
        out_container = QWidget()
        out_container.setLayout(out_row)
        form.addRow("Output:", out_container)

        self.btn_convert = QPushButton("Convert All")
        self.btn_convert.clicked.connect(self.convert_all)

        self.btn_open_out = QPushButton("Open Output Folder")
        self.btn_open_out.setEnabled(False)
        self.btn_open_out.clicked.connect(self.open_output_folder)

        self.progress = QProgressBar()
        self.progress.setValue(0)
        self.status = QLabel("Status: Ready")

        self.modules = QLabel(
            "Modules\n"
            "✓ Images (Free)\n"
            "🔒 PDF (Coming Soon)\n"
            "🔒 Video/Audio (Coming Soon)\n"
            "🔒 Manga/Webtoon (Coming Soon)\n"
            "🔒 Archives (Coming Soon)\n"
            "🔒 Game Textures (Coming Soon)\n"
            "\nTip: Menu → Roadmap"
        )
        self.modules.setStyleSheet("font-size: 11px; color: #999;")

        vright = QVBoxLayout()
        vright.addWidget(right_box)
        vright.addWidget(self.btn_convert)
        vright.addWidget(self.btn_open_out)
        vright.addWidget(self.progress)
        vright.addWidget(self.status)
        vright.addWidget(self.modules)
        vright.addStretch(1)

        right_wrap = QWidget()
        right_wrap.setLayout(vright)
        mid.addWidget(right_wrap, 1)

        self.update_resize_ui()
        self.update_quality_state()

    def show_roadmap(self):
        QMessageBox.information(
            self,
            "Roadmap",
            "Free:\n"
            "- Image convert & resize (offline)\n\n"
            "Coming Soon (Pro):\n"
            "- Presets\n"
            "- PDF tools\n"
            "- Video & Audio\n"
            "- Manga / Webtoon\n"
            "- Archives\n"
            "- Game Textures\n\n"
            "Made by KenoLabs"
        )

    def update_quality_state(self):
        fmt = self.combo_fmt.currentText()
        is_png = (fmt == "PNG")
        self.slider_q.setEnabled(not is_png)
        self.lbl_q.setEnabled(not is_png)

    def update_resize_ui(self):
        mode = self.combo_resize.currentText()
        self.wh_container.setVisible(mode == "Width/Height")
        self.spin_percent.setVisible(mode == "Percent")

    def add_files_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Select images",
            "", "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)"
        )
        self.add_paths([Path(f) for f in files])

    def add_folder_dialog(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder")
        if folder:
            self.add_paths([Path(folder)])

    def pick_output_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output base folder")
        if folder:
            self.edit_out.setText(folder)

    def clear_files(self):
        self.files.clear()
        self.listw.clear()
        self.progress.setValue(0)
        self.status.setText("Status: Ready")
        self.btn_open_out.setEnabled(False)
        self.last_output_dir = None

    def on_drop(self, paths: list):
        self.add_paths([Path(p) for p in paths])

    def add_paths(self, paths: List[Path]):
        added = 0
        for p in paths:
            imgs = collect_images_from_path(p)
            for img in imgs:
                if img not in self.files:
                    self.files.append(img)
                    self.listw.addItem(QListWidgetItem(str(img)))
                    added += 1
        self.status.setText(f"Status: Loaded {len(self.files)} file(s). (+{added})")

    def validate(self) -> Optional[str]:
        if not self.files:
            return "Please add at least 1 image file."
        out_base = self.edit_out.text().strip()
        if not out_base:
            return "Please choose an output folder."
        if not Path(out_base).exists():
            return "Output folder does not exist."
        return None

    def make_auto_output_dir(self, out_base: Path, out_fmt: str) -> Path:
        out_dir = out_base / f"BatchPix_{out_fmt.lower()}"
        safe_mkdir(out_dir)
        return out_dir

    def convert_all(self):
        err = self.validate()
        if err:
            QMessageBox.warning(self, "BatchPix", err)
            return

        out_base = Path(self.edit_out.text().strip())
        out_fmt = self.combo_fmt.currentText()
        out_dir = self.make_auto_output_dir(out_base, out_fmt)
        self.last_output_dir = out_dir
        self.btn_open_out.setEnabled(False)

        resize_text = self.combo_resize.currentText()
        resize_mode = "none" if resize_text == "No resize" else ("wh" if resize_text == "Width/Height" else "percent")

        keep_aspect = self.chk_aspect.isChecked()
        width = self.spin_w.value() if resize_mode == "wh" else None
        height = self.spin_h.value() if resize_mode == "wh" else None
        percent = self.spin_percent.value() if resize_mode == "percent" else None
        quality = self.slider_q.value()

        self.btn_convert.setEnabled(False)
        self.status.setText(f"Status: Converting… (output: {out_dir.name})")
        self.progress.setMaximum(len(self.files))
        self.progress.setValue(0)

        self.worker = ConvertWorker(
            files=self.files,
            out_dir=out_dir,
            out_fmt=out_fmt,
            resize_mode=resize_mode,
            keep_aspect=keep_aspect,
            width=width,
            height=height,
            percent=percent,
            quality=quality
        )
        self.worker.progress.connect(self.on_progress)
        self.worker.finished_ok.connect(self.on_done)
        self.worker.failed.connect(self.on_failed)
        self.worker.start()

    def on_progress(self, done: int, total: int):
        self.progress.setMaximum(total)
        self.progress.setValue(done)
        self.status.setText(f"Status: Converting… ({done}/{total})")

    def on_done(self, out_dir: Path):
        self.btn_convert.setEnabled(True)
        self.status.setText("Status: Done ✅")
        self.last_output_dir = out_dir
        self.btn_open_out.setEnabled(True)
        QMessageBox.information(self, "BatchPix", f"Conversion finished!\n\nOutput:\n{out_dir}")

    def on_failed(self, msg: str):
        self.btn_convert.setEnabled(True)
        self.status.setText("Status: Failed ❌")
        QMessageBox.critical(self, "BatchPix", f"Error:\n{msg}")

    def open_output_folder(self):
        if self.last_output_dir and self.last_output_dir.exists():
            subprocess.Popen(["explorer", str(self.last_output_dir)])
        else:
            base = self.edit_out.text().strip()
            if base:
                subprocess.Popen(["explorer", base])

def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()
