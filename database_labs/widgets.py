"""Общие элементы форм и табличного вывода PySide6."""

from datetime import date, datetime
from decimal import Decimal
from functools import wraps

import psycopg
from psycopg import sql
from PySide6.QtCore import Qt, QSortFilterProxyModel
from PySide6.QtGui import QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSpinBox, QTableView, QVBoxLayout, QWidget,
)

from .db import LOG, error_message
from .queries import COMPARISONS, require


def guarded(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except (psycopg.Error, ValueError) as error:
            LOG.error("%s: %s", method.__name__, error_message(error))
            QMessageBox.warning(self, "Операция не выполнена", error_message(error))
            return None
    return wrapped


def set_options(combo, values, empty=None):
    previous = combo.currentData()
    combo.blockSignals(True)
    combo.clear()
    if empty is not None:
        combo.addItem(empty, "")
    for item in values:
        label, value = item if isinstance(item, tuple) else (item, item)
        combo.addItem(label, value)
    index = next((i for i in range(combo.count()) if combo.itemData(i) == previous), -1)
    combo.setCurrentIndex(index if index >= 0 else (0 if combo.count() else -1))
    combo.blockSignals(False)


def choice(combo):
    return combo.currentData() if combo.currentIndex() >= 0 else ""


def selected(list_widget):
    return [entry.text() for entry in list_widget.selectedItems()]


def select_items(list_widget, values):
    previous = set(selected(list_widget))
    list_widget.clear()
    list_widget.addItems(values)
    for index in range(list_widget.count()):
        item = list_widget.item(index)
        item.setSelected(item.text() in previous)


class Form(QDialog):
    def __init__(self, parent, title, hint="", modal=True):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(modal)
        self.resize(610, 360)
        self.layout = QVBoxLayout(self)
        if hint:
            label = QLabel(hint)
            label.setWordWrap(True)
            self.layout.addWidget(label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body = QWidget()
        self.form = QFormLayout(self.body)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(self.body)
        self.layout.addWidget(scroll, 1)

        self.actions = QHBoxLayout()
        self.layout.addLayout(self.actions)
        self.actions.addStretch()
        self.button("Закрыть", self.reject, footer=True)
        self.values = {}
        self.column_sources = {}

    def add(self, key, label, widget):
        self.values[key] = widget
        self.form.addRow(label, widget)
        return widget

    def text(self, key, label, value="", placeholder=""):
        field = QLineEdit(value)
        field.setPlaceholderText(placeholder)
        return self.add(key, label, field)

    def combo(self, key, label, values=(), empty=None):
        field = QComboBox()
        set_options(field, values, empty)
        return self.add(key, label, field)

    def spin(self, key, label, value, minimum=1, maximum=10_000):
        field = QSpinBox()
        field.setRange(minimum, maximum)
        field.setValue(value)
        return self.add(key, label, field)

    def check(self, key, label, value=False):
        field = QCheckBox()
        field.setChecked(value)
        return self.add(key, label, field)

    def list(self, key, label, values=()):
        field = QListWidget()
        field.setSelectionMode(QAbstractItemView.SelectionMode.MultiSelection)
        field.setMaximumHeight(105)
        field.addItems(values)
        return self.add(key, label, field)

    def button(self, label, callback, footer=False):
        button = QPushButton(label)
        button.setAutoDefault(False)
        button.clicked.connect(lambda: callback())
        if footer:
            self.actions.insertWidget(0, button)
        else:
            self.form.addRow(button)
        return button

    def get(self, key):
        widget = self.values[key]
        if isinstance(widget, QComboBox):
            return choice(widget)
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QSpinBox):
            return widget.value()
        if isinstance(widget, QListWidget):
            return selected(widget)
        return widget.text()

    def required(self, key, message):
        return require(self.get(key), message)

    def hide_row(self, key, hidden=True):
        self.form.setRowVisible(self.values[key], not hidden)

    def column_combo(self, db, source, key, label, empty=None):
        widget = self.combo(key, label)
        self.bind_columns(db, source, widget, empty)
        return widget

    def column_list(self, db, source, key, label):
        widget = self.list(key, label)
        self.bind_columns(db, source, widget)
        return widget

    def bind_columns(self, db, source, widget, empty=None):
        if source not in self.column_sources:
            self.column_sources[source] = []
            self.values[source].currentIndexChanged.connect(
                lambda _index: self.refresh_columns(db, source)
            )
        self.column_sources[source].append((widget, empty))
        self.refresh_columns(db, source)

    @guarded
    def refresh_columns(self, db, source):
        fields = [field for field, in db.columns(self.get(source))]
        for widget, empty in self.column_sources[source]:
            if isinstance(widget, QListWidget):
                select_items(widget, fields)
            else:
                set_options(widget, fields, empty)

    def filter_form(self, db, source, prefix="", operators=COMPARISONS, label="Поле WHERE"):
        self.column_combo(db, source, prefix + "filter", label, "Без условия")
        self.combo(prefix + "op", "Оператор", operators)
        self.text(prefix + "value", "Значение")

    def filter_data(self, prefix=""):
        field = self.get(prefix + "filter")
        if field:
            return field, self.get(prefix + "op"), self.get(prefix + "value")
        return None

    def text_box(self, label, content=""):
        box = QPlainTextEdit(content)
        box.setReadOnly(True)
        box.setMaximumHeight(100)
        self.form.addRow(label, box)
        return box


class ResultDialog(QDialog):
    def __init__(self, parent, title, headers, rows, statement=""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1040, 550)
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        layout.addLayout(controls)
        filter_field = QComboBox()
        filter_field.addItem("Все столбцы", -1)
        for index, header in enumerate(headers):
            filter_field.addItem(header, index)
        filter_text = QLineEdit()
        filter_text.setPlaceholderText("Фильтр по загруженным строкам")
        controls.addWidget(filter_field)
        controls.addWidget(filter_text, 1)
        status = QLabel()
        controls.addWidget(status)

        table = QTableView()
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(table, 1)
        model = QStandardItemModel(self)
        model.setHorizontalHeaderLabels(headers)
        sort_role = Qt.ItemDataRole.UserRole + 1
        for row in rows[:1000]:
            items = []
            for value in row:
                if isinstance(value, bool):
                    display, order = ("Да" if value else "Нет"), int(value)
                elif isinstance(value, (int, float, Decimal)):
                    display, order = str(value), float(value)
                elif isinstance(value, (date, datetime)):
                    display = order = value.isoformat()
                else:
                    display = order = "NULL" if value is None else str(value)
                item = QStandardItem(display)
                item.setData(order, sort_role)
                items.append(item)
            model.appendRow(items)

        proxy = QSortFilterProxyModel(self)
        proxy.setSourceModel(model)
        proxy.setSortRole(sort_role)
        proxy.setSortCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        proxy.setFilterCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        table.setModel(proxy)
        table.setSortingEnabled(True)
        table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        table.resizeColumnsToContents()
        for index in range(len(headers)):
            table.setColumnWidth(index, max(100, min(table.columnWidth(index), 340)))

        def apply_filter():
            proxy.setFilterKeyColumn(filter_field.currentData())
            proxy.setFilterFixedString(filter_text.text())
            limit = " · показаны первые 1000" if len(rows) > 1000 else ""
            status.setText(f"Строк: {proxy.rowCount()} из {model.rowCount()}{limit}")

        filter_field.currentIndexChanged.connect(apply_filter)
        filter_text.textChanged.connect(apply_filter)
        apply_filter()

        if statement:
            box = QPlainTextEdit(statement.strip())
            box.setReadOnly(True)
            box.setMaximumHeight(96)
            layout.addWidget(box)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.accept)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)


def show_query(parent, db, title, query, params=()):
    statement = query.as_string(db.connection)
    headers, rows = db.execute(title, query + sql.SQL(" LIMIT 1001"), params)
    ResultDialog(parent, title, headers, rows, statement).exec()
