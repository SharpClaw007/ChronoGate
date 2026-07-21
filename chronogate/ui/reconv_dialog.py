"""The IRF-reconvolution fit dialog: choose the IRF, model and fit scope.

Collects everything :func:`chronogate.reconv.fit_decay` / ``fit_map`` need for a
rigorous, IRF-deconvolved lifetime: where the instrument response comes from
(a measured IRF file or a Gaussian model), the number of exponential components,
the fitting objective, a photon threshold, and whether to fit just the selected
region (one fit) or the whole image (a per-pixel τ-map). Purely declarative: the
caller reads the accessors after ``exec()``.
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
)


class ReconvDialog(QDialog):
    """Collects IRF-reconvolution fit settings.

    Accessors (read after ``exec()``): :meth:`irf_is_gaussian`,
    :meth:`gaussian_center_ns`, :meth:`gaussian_fwhm_ns`, :meth:`irf_path`,
    :meth:`model` ("mono"/"bi"), :meth:`objective` ("mle"/"chi2"),
    :meth:`photon_threshold`, :meth:`fit_map` (True = whole-image τ-map).
    """

    def __init__(self, parent=None, *, has_selection: bool = False,
                 resolution_ns: float = 0.0, default_center_ns: float = 0.0) -> None:
        super().__init__(parent)
        self.setWindowTitle("IRF lifetime fit")

        # --- IRF source ---
        irf_box = QGroupBox("Instrument response (IRF)")
        self.rb_gauss = QRadioButton("Gaussian model")
        self.rb_measured = QRadioButton("Measured IRF file…")
        self.rb_gauss.setChecked(True)
        self._irf_grp = QButtonGroup(self)
        self._irf_grp.addButton(self.rb_gauss)
        self._irf_grp.addButton(self.rb_measured)

        self.sp_center = QDoubleSpinBox()
        self.sp_center.setRange(0.0, 1e6); self.sp_center.setDecimals(3)
        self.sp_center.setSuffix(" ns"); self.sp_center.setValue(float(default_center_ns))
        self.sp_fwhm = QDoubleSpinBox()
        self.sp_fwhm.setRange(1e-4, 1e6); self.sp_fwhm.setDecimals(3)
        self.sp_fwhm.setSuffix(" ns")
        # A sensible default FWHM: a few TCSPC bins if we know the resolution.
        self.sp_fwhm.setValue(max(3.0 * float(resolution_ns), 0.1) if resolution_ns else 0.2)

        self.ed_path = QLineEdit(); self.ed_path.setPlaceholderText("no IRF file selected")
        self.ed_path.setReadOnly(True)
        self.btn_browse = QPushButton("Browse…")
        self.btn_browse.clicked.connect(self._browse_irf)

        gform = QFormLayout()
        gform.addRow(self.rb_gauss)
        gform.addRow("  centre", self.sp_center)
        gform.addRow("  FWHM", self.sp_fwhm)
        gform.addRow(self.rb_measured)
        prow = QHBoxLayout(); prow.addWidget(self.ed_path); prow.addWidget(self.btn_browse)
        gform.addRow("  file", prow)
        irf_box.setLayout(gform)
        self.rb_gauss.toggled.connect(self._sync_irf_enabled)
        self._sync_irf_enabled()

        # --- model / estimator ---
        fit_box = QGroupBox("Model")
        self.cb_model = QComboBox(); self.cb_model.addItems(["mono (1 exponential)",
                                                             "bi (2 exponentials)"])
        self.cb_obj = QComboBox(); self.cb_obj.addItems(["Poisson MLE (recommended)",
                                                         "weighted χ²"])
        self.sp_thresh = QSpinBox(); self.sp_thresh.setRange(0, 10_000_000)
        self.sp_thresh.setValue(100)
        self.sp_thresh.setToolTip("Pixels with fewer total photons are left "
                                  "unfitted (NaN) — this keeps a per-pixel fit tractable.")
        fform = QFormLayout()
        fform.addRow("components", self.cb_model)
        fform.addRow("objective", self.cb_obj)
        fform.addRow("photon threshold", self.sp_thresh)
        fit_box.setLayout(fform)

        # --- scope ---
        scope_box = QGroupBox("Fit scope")
        self.rb_region = QRadioButton("Selected region — one fit (fast)")
        self.rb_map = QRadioButton("Whole image — per-pixel τ-map (slow)")
        self._scope_grp = QButtonGroup(self)
        self._scope_grp.addButton(self.rb_region)
        self._scope_grp.addButton(self.rb_map)
        self.rb_region.setChecked(has_selection)
        self.rb_map.setChecked(not has_selection)
        self.rb_region.setEnabled(has_selection)
        if not has_selection:
            self.rb_region.setToolTip("Select pixels/a region first to enable a single region fit.")
        svb = QVBoxLayout(); svb.addWidget(self.rb_region); svb.addWidget(self.rb_map)
        scope_box.setLayout(svb)

        note = QLabel("Reconvolution τ is IRF-deconvolved — distinct from the "
                      "fit-free RLD τ and phasor. The report labels each by method.")
        note.setWordWrap(True)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept); bb.rejected.connect(self.reject)

        root = QVBoxLayout(self)
        root.addWidget(irf_box); root.addWidget(fit_box)
        root.addWidget(scope_box); root.addWidget(note); root.addWidget(bb)

    # -- internal --
    def _sync_irf_enabled(self) -> None:
        gauss = self.rb_gauss.isChecked()
        self.sp_center.setEnabled(gauss); self.sp_fwhm.setEnabled(gauss)
        self.ed_path.setEnabled(not gauss); self.btn_browse.setEnabled(not gauss)

    def _browse_irf(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose an IRF measurement",
            "", "FLIM data (*.ptu *.sdt);;All files (*)")
        if path:
            self.ed_path.setText(path)
            self.rb_measured.setChecked(True)

    # -- accessors --
    def irf_is_gaussian(self) -> bool:
        return self.rb_gauss.isChecked()

    def gaussian_center_ns(self) -> float:
        return float(self.sp_center.value())

    def gaussian_fwhm_ns(self) -> float:
        return float(self.sp_fwhm.value())

    def irf_path(self) -> str:
        return self.ed_path.text().strip()

    def model(self) -> str:
        return "bi" if self.cb_model.currentIndex() == 1 else "mono"

    def objective(self) -> str:
        return "chi2" if self.cb_obj.currentIndex() == 1 else "mle"

    def photon_threshold(self) -> float:
        return float(self.sp_thresh.value())

    def fit_map(self) -> bool:
        """True to fit the whole image (τ-map); False for a single region fit."""
        return self.rb_map.isChecked()
