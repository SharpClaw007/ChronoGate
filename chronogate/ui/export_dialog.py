"""The export dialog: pick the artefacts, their parameters and the folder.

One dialog serves both File ▸ Export and File ▸ Export all planes (batch) --
the latter just arrives with the batch box pre-checked. The provenance JSON is
deliberately not optional (an export you cannot reproduce is not an export),
so it is stated as a fact rather than offered as a checkbox.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from ..export import ExportOptions

# Above this many selected pixels the per-pixel CSV starts unchecked: it is
# never capped (it is the data the user asked for), but a many-megabyte table
# should be written knowingly, not by surprise.
PIXEL_TABLE_WARN_ROWS = 100_000


class ExportDialog(QDialog):
    """Collects an :class:`~chronogate.export.ExportOptions`, an output folder
    and a single-vs-batch choice. Purely declarative: the caller reads
    :meth:`options`, :meth:`out_dir` and :meth:`batch` after ``exec()``."""

    def __init__(self, parent=None, *, raster_label: str = "gated intensity",
                 has_selection: bool = False, sel_rows: int = 0, sel_bytes: int = 0,
                 warn_rows: int = PIXEL_TABLE_WARN_ROWS, n_planes: int = 1,
                 default_dir: str = "", batch: bool = False) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export")

        what = QGroupBox("What to export")
        self.chk_raw = QCheckBox(f"Raster TIFF — raw {raster_label} values (for ImageJ)")
        self.chk_png = QCheckBox("Colormapped PNG with colorbar (for figures)")
        self.chk_decay = QCheckBox("Summed decay CSV (time_ns, counts)")
        self.chk_sel = QCheckBox("Selection files — label-map TIFF + pooled-decay CSV")
        self.chk_pixels = QCheckBox(self._pixel_table_text(sel_rows, sel_bytes))
        for c in (self.chk_raw, self.chk_png, self.chk_decay):
            c.setChecked(True)
        self.chk_sel.setChecked(has_selection)
        self.chk_sel.setEnabled(has_selection)
        # A huge table starts opted out -- announced, never capped.
        self.chk_pixels.setChecked(has_selection and sel_rows <= warn_rows)
        self.chk_pixels.setEnabled(has_selection)
        if not has_selection:
            self.chk_sel.setToolTip("No selection to export -- pick pixels, draw an "
                                    "ROI or lasso the phasor first.")
            self.chk_pixels.setToolTip(self.chk_sel.toolTip())
        # The pixel table describes the selection files' pixels; without them it
        # has no label map to refer to, so it follows its parent.
        self.chk_sel.toggled.connect(self._sync_pixels_enabled)

        wl = QVBoxLayout(what)
        for c in (self.chk_raw, self.chk_png, self.chk_decay, self.chk_sel):
            wl.addWidget(c)
        pix_row = QHBoxLayout()
        pix_row.addSpacing(22)                 # indent: child of the selection box
        pix_row.addWidget(self.chk_pixels)
        wl.addLayout(pix_row)
        prov_note = QLabel("The provenance JSON (settings, metadata, omissions) "
                           "is always written.")
        prov_note.setStyleSheet("color: palette(mid);")
        wl.addWidget(prov_note)

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("Folder"))
        self.dir_edit = QLineEdit(default_dir)
        self.dir_edit.setToolTip("Where the files are written (created if needed).")
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._browse)
        folder_row.addWidget(self.dir_edit, 1)
        folder_row.addWidget(self.btn_browse)

        self.chk_batch = QCheckBox(f"Apply to all {n_planes} planes (batch)")
        self.chk_batch.setEnabled(n_planes > 1)
        self.chk_batch.setChecked(batch and n_planes > 1)
        self.chk_batch.setToolTip(
            "Export every plane of the stack with the current gate/threshold/mode; "
            "selections are re-cut per plane." if n_planes > 1
            else "A single file is loaded; there is no stack to batch over.")

        self.buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        self.buttons.button(QDialogButtonBox.Ok).setText("Export")
        self.dir_edit.textChanged.connect(self._sync_ok_enabled)
        self._sync_ok_enabled()

        lay = QVBoxLayout(self)
        lay.addWidget(what)
        lay.addLayout(folder_row)
        lay.addWidget(self.chk_batch)
        lay.addWidget(self.buttons)

    @staticmethod
    def _pixel_table_text(rows: int, nbytes: int) -> str:
        base = "Per-pixel metric table CSV"
        if rows <= 0:
            return base
        mb = max(1, round(nbytes / 1e6)) if nbytes >= 5e5 else None
        size = f", ~{mb} MB" if mb else ""
        return f"{base} ({rows:,} pixels{size})"

    def _sync_pixels_enabled(self, on: bool) -> None:
        self.chk_pixels.setEnabled(on)
        if not on:
            self.chk_pixels.setChecked(False)

    def _sync_ok_enabled(self) -> None:
        self.buttons.button(QDialogButtonBox.Ok).setEnabled(bool(self.dir_edit.text().strip()))

    def _browse(self) -> None:
        d = QFileDialog.getExistingDirectory(
            self, "Choose an output folder", self.dir_edit.text(),
            QFileDialog.Option.ShowDirsOnly | QFileDialog.Option.DontUseNativeDialog)
        if d:
            self.dir_edit.setText(d)

    def options(self) -> ExportOptions:
        return ExportOptions(
            raw_tiff=self.chk_raw.isChecked(),
            color_png=self.chk_png.isChecked(),
            decay_csv=self.chk_decay.isChecked(),
            selection=self.chk_sel.isChecked(),
            pixel_table=self.chk_pixels.isChecked(),
        )

    def out_dir(self) -> str:
        return self.dir_edit.text().strip()

    def batch(self) -> bool:
        return self.chk_batch.isChecked()
