# -*- coding: utf-8 -*-
import sys
import requests
from datetime import datetime, timedelta, date
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

# ---------- HTTP / Daten ----------

CURRENCY_DEFAULT = "EUR"
RYR_URL = "https://www.ryanair.com/api/farfnd/v4/oneWayFares/{}/{}/cheapestPerDay"
HEADERS = {"Accept": "application/json", "User-Agent": "RoundtripFinder/1.3 (PySide6)"}


def fetch_cheapest_per_day_map(origin_iata: str, dest_iata: str, y: int, m: int, curr: str = CURRENCY_DEFAULT) -> Dict[str, dict]:
    """Holt fuer Monat y-m die cheapestPerDay-Daten.
       Rueckgabe: dict[YYYY-MM-DD] = { price: float, dep: str|None, arr: str|None }"""
    month_str = f"{y:04d}-{m:02d}-01"
    url = RYR_URL.format(origin_iata, dest_iata)
    params = {"outboundMonthOfDate": month_str, "currency": curr}
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        data = r.json()
    except requests.RequestException:
        return {}
    except ValueError:
        return {}
    outbound = data.get("outbound", {}) or {}
    fares = outbound.get("fares", []) or []
    result = {}
    for f in fares:
        if f.get("unavailable") or not f.get("price"):
            continue
        day = f.get("day")
        p = f["price"].get("value")
        dep = f.get("departureDate")
        arr = f.get("arrivalDate")
        if day and isinstance(p, (int, float)):
            result[day] = {"price": float(p), "dep": dep, "arr": arr}
    return result


def hhmm(ts: Optional[str]) -> str:
    if not ts:
        return "-"
    try:
        return ts.split("T")[1][:5]
    except Exception:
        return "-"


@dataclass
class DayQuote:
    day: str
    price: float
    depISO: Optional[str]
    arrISO: Optional[str]


@dataclass
class Candidate:
    total: float
    out_day: str
    ret_day: str
    out_price: float
    ret_price: float
    dep_o: Optional[str]
    arr_o: Optional[str]
    dep_r: Optional[str]
    arr_r: Optional[str]
    route_label: str


# ---------- Roundtrip-Suche (Datumsspanne, jahresübergreifend) ----------

def months_between(start: date, end: date) -> List[Tuple[int, int]]:
    """Liefert (Jahr, Monat) für alle Monate, die die Datumsspanne [start, end] überdecken."""
    ym = []
    y, m = start.year, start.month
    while True:
        ym.append((y, m))
        if y == end.year and m == end.month:
            break
        m += 1
        if m == 13:
            m = 1
            y += 1
    return ym


def find_roundtrips_for_route_by_dates(origin: str, dest: str, start_date: date, end_date: date,
                                       min_days: int, max_days: int, currency: str,
                                       month_cache: Dict[Tuple[str, str, int, int, str], Dict[str, DayQuote]]) -> List[Candidate]:
    """Erzeugt Roundtrip-Kandidaten für eine Route innerhalb einer exakten Datumsspanne.
    Bedingung: Abflug- und Rückflugdatum müssen innerhalb [start_date, end_date] liegen.
    Lädt die nötigen Monate dynamisch nach (auch jahresübergreifend).
    """

    def get_month_map(o: str, d: str, y: int, m: int, c: str) -> Dict[str, DayQuote]:
        key = (o, d, y, m, c)
        if key in month_cache:
            return month_cache[key]
        raw = fetch_cheapest_per_day_map(o, d, y, m, c)
        mapped = {k: DayQuote(k, v["price"], v.get("dep"), v.get("arr")) for k, v in raw.items()}
        month_cache[key] = mapped
        return mapped

    # Alle relevanten Monate laden (Hin- und Rückroute)
    ym_list = months_between(start_date, end_date)
    out_maps: Dict[Tuple[int, int], Dict[str, DayQuote]] = {}
    ret_maps: Dict[Tuple[int, int], Dict[str, DayQuote]] = {}

    for (y, m) in ym_list:
        out_maps[(y, m)] = get_month_map(origin, dest, y, m, currency)
    for (y, m) in ym_list:
        ret_maps[(y, m)] = get_month_map(dest, origin, y, m, currency)

    cands: List[Candidate] = []
    for fmap in out_maps.values():
        for out_day, info in fmap.items():
            try:
                out_dt = datetime.strptime(out_day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if not (start_date <= out_dt <= end_date):
                continue
            for span in range(min_days, max_days + 1):
                ret_dt = out_dt + timedelta(days=span)
                if not (start_date <= ret_dt <= end_date):
                    continue
                ret_map = ret_maps.get((ret_dt.year, ret_dt.month), {})
                ret_day = ret_dt.strftime("%Y-%m-%d")
                if ret_day not in ret_map:
                    continue
                out_price = info.price
                ret_price = ret_map[ret_day].price
                total = out_price + ret_price
                cands.append(
                    Candidate(total, out_day, ret_day, out_price, ret_price,
                              info.depISO, info.arrISO, ret_map[ret_day].depISO, ret_map[ret_day].arrISO,
                              f"{origin} ↔ {dest}")
                )
    cands.sort(key=lambda x: (x.total, x.out_day))
    return cands


# ---------- GUI ----------

from PySide6.QtCore import Qt, QThread, Signal, QObject, QDate
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QHBoxLayout, QVBoxLayout,
    QLabel, QComboBox, QLineEdit, QSpinBox, QPushButton,
    QGroupBox, QTableWidget, QTableWidgetItem, QSplitter, QMessageBox,
    QTabWidget, QScrollArea, QDateEdit
)

# Matplotlib-Backend für PySide6
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
import matplotlib.dates as mdates


class PriceChart(FigureCanvas):
    """Linienchart: pro Route die (pro Abflugtag) besten Gesamtpreise."""

    def __init__(self, parent=None):
        fig = Figure(figsize=(6, 4), tight_layout=True)
        self.ax = fig.add_subplot(111)
        super().__init__(fig)
        self.setParent(parent)

    def plot_routes(self, series: Dict[str, List[Tuple[date, float]]]):
        self.ax.clear()
        for label, data in series.items():
            if not data:
                continue
            data_sorted = sorted(data, key=lambda t: t[0])
            xs = [mdates.date2num(d) for d, _ in data_sorted]
            ys = [p for _, p in data_sorted]
            # Keine expliziten Farben festlegen -> Matplotlib-Default
            self.ax.plot_date(xs, ys, linestyle='solid', marker=None, label=label)
        self.ax.set_title("Beste Gesamtpreise pro Abflugtag (Routenvergleich)")
        self.ax.set_xlabel("Abflugdatum")
        self.ax.set_ylabel("Gesamtpreis")
        self.ax.grid(True, which="both", alpha=0.3)
        self.ax.legend()
        self.ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m-%d'))
        self.ax.tick_params(axis='x', rotation=45)
        self.draw()


class Worker(QObject):
    finished = Signal(dict, list, dict, str)  # per_route, combined, chart_series, error

    def __init__(self, params):
        super().__init__()
        self.params = params

    def run(self):
        p = self.params
        currency = p["currency"]
        min_days = p["min_days"]
        max_days = p["max_days"]
        routes = p["routes"]
        top_n = p["top_n"]

        month_cache: Dict[Tuple[str, str, int, int, str], Dict[str, DayQuote]] = {}
        per_route: Dict[str, List[Candidate]] = {}
        all_cands: List[Candidate] = []
        chart_series: Dict[str, List[Tuple[date, float]]] = {}
        error = ""

        try:
            start_date: date = p["start_date"]
            end_date: date = p["end_date"]
            for (o, d) in routes:
                label = f"{o} ↔ {d}"
                cands = find_roundtrips_for_route_by_dates(o, d, start_date, end_date, min_days, max_days, currency, month_cache)
                per_route[label] = cands[:top_n]
                all_cands.extend(cands)

                best_per_day: Dict[str, float] = {}
                for c in cands:
                    best_per_day[c.out_day] = min(best_per_day.get(c.out_day, float('inf')), c.total)
                points: List[Tuple[date, float]] = []
                for day_str, price in best_per_day.items():
                    try:
                        dt = datetime.strptime(day_str, "%Y-%m-%d").date()
                        points.append((dt, price))
                    except ValueError:
                        pass
                chart_series[label] = points

            combined: List[Candidate] = []
            if len(routes) >= 2 and all_cands:
                all_cands.sort(key=lambda x: (x.total, x.out_day))
                combined = all_cands[:top_n]

        except Exception as e:
            error = str(e)

        self.finished.emit(per_route, combined, chart_series, error)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Roundtrip-Finder (Windows)")
        self.resize(1280, 780)

        # --- Controls (links) ---
        self.mode = QComboBox()
        self.mode.addItems(["A: CGN↔PMO & NRN↔TPS", "B: 2 eigene Routen", "C: 1 Route"])

        self.o1 = QLineEdit("CGN"); self.d1 = QLineEdit("PMO")
        self.o2 = QLineEdit("NRN"); self.d2 = QLineEdit("TPS")
        for w in (self.o1, self.d1, self.o2, self.d2):
            w.setMaxLength(3); w.setPlaceholderText("IATA"); w.setInputMask(">AAA;_")

        # Datumsspanne (einziger Modus)
        today = date.today()
        self.dStart = QDateEdit(); self.dStart.setDisplayFormat("yyyy-MM-dd")
        self.dStart.setCalendarPopup(True)
        self.dStart.setDate(QDate(today.year, today.month, today.day))

        self.dEnd = QDateEdit(); self.dEnd.setDisplayFormat("yyyy-MM-dd")
        self.dEnd.setCalendarPopup(True)
        default_end = today + timedelta(days=30)
        self.dEnd.setDate(QDate(default_end.year, default_end.month, default_end.day))

        self.minDays = QSpinBox(); self.minDays.setRange(1, 90); self.minDays.setValue(3)
        self.maxDays = QSpinBox(); self.maxDays.setRange(1, 360); self.maxDays.setValue(14)
        self.topN = QSpinBox(); self.topN.setRange(1, 50); self.topN.setValue(5)

        self.currency = QComboBox(); self.currency.addItems(["EUR", "GBP", "PLN", "USD"])

        self.btnSearch = QPushButton("Suchen")
        self.btnSearch.clicked.connect(self.on_search)

        # --- Rechter Bereich: Tabs ---
        self.tabs = QTabWidget()

        # Tab 1: Ergebnisse (Tabellen) + Scroll
        self.results_container = QWidget()
        self.results_layout = QVBoxLayout(self.results_container)
        self.results_layout.setContentsMargins(8, 8, 8, 8)
        self.results_layout.setSpacing(8)

        self.results_scroll = QScrollArea()
        self.results_scroll.setWidgetResizable(True)
        self.results_scroll.setWidget(self.results_container)

        self.tabs.addTab(self.results_scroll, "Ergebnisse")

        # Tab 2: Diagramm
        self.chart = PriceChart()
        chart_wrap = QWidget()
        chart_lay = QVBoxLayout(chart_wrap)
        chart_lay.setContentsMargins(8, 8, 8, 8)
        chart_lay.addWidget(self.chart)
        self.tabs.addTab(chart_wrap, "Diagramm")

        # Layout links (Eingaben)
        left = QVBoxLayout()
        g_mode = QGroupBox("Modus")
        lm = QVBoxLayout(g_mode)
        lm.addWidget(self.mode)
        left.addWidget(g_mode)

        g_routes = QGroupBox("Routen")
        grid = QGridLayout(g_routes)
        grid.addWidget(QLabel("Route 1:"), 0, 0)
        grid.addWidget(self.o1, 0, 1); grid.addWidget(QLabel("→"), 0, 2); grid.addWidget(self.d1, 0, 3)
        grid.addWidget(QLabel("Route 2:"), 1, 0)
        grid.addWidget(self.o2, 1, 1); grid.addWidget(QLabel("→"), 1, 2); grid.addWidget(self.d2, 1, 3)
        left.addWidget(g_routes)

        g_time = QGroupBox("Zeitraum & Optionen")
        gt = QGridLayout(g_time)

        gt.addWidget(QLabel("Start-Tag"), 0, 0); gt.addWidget(self.dStart, 0, 1)
        gt.addWidget(QLabel("End-Tag"), 0, 2);   gt.addWidget(self.dEnd, 0, 3)

        gt.addWidget(QLabel("Min. Tage"), 1, 0); gt.addWidget(self.minDays, 1, 1)
        gt.addWidget(QLabel("Max. Tage"), 1, 2); gt.addWidget(self.maxDays, 1, 3)
        gt.addWidget(QLabel("Top-N"), 1, 4);     gt.addWidget(self.topN, 1, 5)
        gt.addWidget(QLabel("Währung"), 2, 0);   gt.addWidget(self.currency, 2, 1)
        left.addWidget(g_time)

        left.addWidget(self.btnSearch)
        left.addStretch(1)

        # Splitter: links Eingaben, rechts Tabs
        splitter = QSplitter()
        left_widget = QWidget(); left_widget.setLayout(left)
        splitter.addWidget(left_widget)
        splitter.addWidget(self.tabs)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)

        central = QWidget(); lay = QHBoxLayout(central); lay.addWidget(splitter); self.setCentralWidget(central)

        # Modus-Logik
        self.mode.currentIndexChanged.connect(self.update_mode_fields)
        self.update_mode_fields()

    def update_mode_fields(self):
        idx = self.mode.currentIndex()
        # Modus A: feste Routen
        if idx == 0:
            self.o1.setText("CGN"); self.d1.setText("PMO")
            self.o2.setText("NRN"); self.d2.setText("TPS")
            self.o1.setEnabled(False); self.d1.setEnabled(False)
            self.o2.setEnabled(False); self.d2.setEnabled(False)
        elif idx == 1:  # zwei eigene Routen
            self.o1.setEnabled(True); self.d1.setEnabled(True)
            self.o2.setEnabled(True); self.d2.setEnabled(True)
        else:  # eine Route
            self.o1.setEnabled(True); self.d1.setEnabled(True)
            self.o2.setEnabled(False); self.d2.setEnabled(False)

    def clear_tables(self):
        while self.results_layout.count():
            item = self.results_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

    def add_table(self, title: str, rows: List[Candidate]):
        label = QLabel(title)
        label.setStyleSheet("font-weight:600; margin-top:8px;")
        self.results_layout.addWidget(label)

        table = QTableWidget()
        table.setColumnCount(6)
        table.setHorizontalHeaderLabels(["Total", "Out-Date", "Ret-Date", "Out € / Zeit", "Ret € / Zeit", "Route"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setRowCount(len(rows))

        for r, c in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(f"{c.total:.2f}"))
            table.setItem(r, 1, QTableWidgetItem(c.out_day))
            table.setItem(r, 2, QTableWidgetItem(c.ret_day))
            table.setItem(r, 3, QTableWidgetItem(f"{c.out_price:.2f} | {hhmm(c.dep_o)}→{hhmm(c.arr_o)}"))
            table.setItem(r, 4, QTableWidgetItem(f"{c.ret_price:.2f} | {hhmm(c.dep_r)}→{hhmm(c.arr_r)}"))
            table.setItem(r, 5, QTableWidgetItem(c.route_label))

        table.resizeColumnsToContents()
        self.results_layout.addWidget(table)

    def on_search(self):
        # Validierung Datumsspanne
        start_qd: QDate = self.dStart.date()
        end_qd: QDate = self.dEnd.date()
        start_date = date(start_qd.year(), start_qd.month(), start_qd.day())
        end_date = date(end_qd.year(), end_qd.month(), end_qd.day())
        if end_date < start_date:
            QMessageBox.warning(self, "Eingabe", "End-Tag muss ≥ Start-Tag sein.")
            return

        if self.maxDays.value() < self.minDays.value():
            QMessageBox.warning(self, "Eingabe", "Max. Tage muss ≥ Min. Tage sein.")
            return

        idx = self.mode.currentIndex()
        routes: List[Tuple[str, str]] = []
        if idx == 0:   # A
            routes = [("CGN", "PMO"), ("NRN", "TPS")]
        elif idx == 1:  # B
            routes = [(self.o1.text().upper(), self.d1.text().upper()),
                      (self.o2.text().upper(), self.d2.text().upper())]
        else:           # C
            routes = [(self.o1.text().upper(), self.d1.text().upper())]

        # Thread-Worker starten
        params = dict(
            currency=self.currency.currentText(),
            min_days=self.minDays.value(),
            max_days=self.maxDays.value(),
            top_n=self.topN.value(),
            routes=routes,
            start_date=start_date,
            end_date=end_date,
        )

        self.btnSearch.setEnabled(False)
        self.clear_tables()
        self.chart.plot_routes({})  # leeren

        self.thread = QThread()
        self.worker = Worker(params)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.finished.connect(self.on_worker_finished)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    def on_worker_finished(self, per_route: dict, combined: list, chart_series: dict, error: str):
        self.btnSearch.setEnabled(True)
        if error:
            QMessageBox.critical(self, "Fehler", error or "Unbekannter Fehler")
            return

        # Tabellen rendern (Tab "Ergebnisse")
        for label, rows in per_route.items():
            self.add_table(f"Top {self.topN.value()} – {label}", rows)
        if combined:
            self.add_table("GESAMT-RANKING", combined)

        # Diagramm (Tab "Diagramm")
        self.chart.plot_routes(chart_series)


def main():
    app = QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
