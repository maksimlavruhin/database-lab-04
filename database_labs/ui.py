"""Главное окно и формы первой лабораторной."""

import getpass
import os
import sys
from datetime import date
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg import sql
from PySide6.QtCore import QDate
from PySide6.QtWidgets import (
    QApplication, QDateEdit, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .db import Database, LOG, configure_logging
from .queries import require
from .widgets import Form, ResultDialog, guarded


DATASET_KINDS = (("Отзывы", "reviews"), ("Комментарии", "comments"), ("Сообщения", "messages"))
LAB_TITLES = (
    "Схема и целостность данных",
    "ALTER TABLE, SELECT и JOIN",
    "Подзапросы, типы и выражения",
    "Расширенная группировка, представления и CTE",
    "Оконные функции",
)


def add_days_field(form, key, label, today=False):
    field = QDateEdit(QDate.currentDate() if today else QDate(1900, 1, 1))
    field.setDisplayFormat("yyyy-MM-dd")
    field.setCalendarPopup(True)
    if not today:
        field.setMinimumDate(QDate(1900, 1, 1))
        field.setSpecialValueText("Любая")
    return form.add(key, label, field)


def metric(value):
    try:
        number = Decimal(value.replace(",", "."))
    except InvalidOperation as error:
        raise ValueError("Введите F1 в виде числа, например 0,85.") from error
    if not number.is_finite():
        raise ValueError("Введите конечное число для F1.")
    return number


class InsertDialog(Form):
    def __init__(self, parent, db, kind):
        titles = {"datasets": "Добавить датасет", "models": "Добавить модель",
                  "experiments": "Добавить эксперимент"}
        super().__init__(parent, titles[kind])
        self.db = db
        self.kind = kind
        if kind == "datasets":
            self.text("name", "Название")
            self.combo("kind", "Тип датасета", DATASET_KINDS)
            self.text("labels", "Метки через запятую", placeholder="обычный, токсичный")
            self.check("is_public", "Публичный")
        elif kind == "models":
            self.text("name", "Название")
            self.check("is_active", "Активная", True)
        else:
            self.text("title", "Название")
            self.combo("dataset_id", "Датасет", self.references("datasets"), "Выберите датасет")
            self.combo("model_id", "Модель", self.references("models"), "Выберите модель")
            self.date_field = add_days_field(self, "run_date", "Дата запуска", today=True)
            self.text("f1", "F1", placeholder="0,85")
            self.spin("samples", "Размер выборки", 100, 0, 2_147_483_647)
            self.text("notes", "Заметка")
        self.button("Сохранить", self.save, footer=True)

    def references(self, table):
        query = sql.SQL("SELECT name, id FROM {} ORDER BY name").format(self.db.relation(table))
        return self.db.execute(f"SELECT {table} для формы ввода", query)[1]

    @guarded
    def save(self):
        if self.kind == "datasets":
            values = {
                "name": self.get("name").strip() or None,
                "kind": self.get("kind"),
                "labels": [tag.strip() for tag in self.get("labels").split(",") if tag.strip()],
                "is_public": self.get("is_public"),
            }
        elif self.kind == "models":
            values = {"name": self.get("name").strip() or None, "is_active": self.get("is_active")}
        else:
            values = {
                "title": self.get("title").strip() or None,
                "dataset_id": self.required("dataset_id", "Выберите датасет."),
                "model_id": self.required("model_id", "Выберите модель."),
                "run_date": self.date_field.date().toPython(),
                "f1": metric(self.get("f1")) if self.get("f1").strip() else None,
                "samples": self.get("samples"),
                "notes": self.get("notes").strip() or None,
            }
        record_id = self.db.insert(self.kind, values)
        QMessageBox.information(self, "Запись добавлена", f"Идентификатор: {record_id}")
        self.accept()


class SummaryDialog(Form):
    def __init__(self, parent, db, level):
        super().__init__(parent, "Вывод данных", "Сводка объединяет эксперименты, модели и датасеты.")
        self.db = db
        self.level = level
        self.combo("kind", "Тип датасета", DATASET_KINDS, "Все типы")
        self.start = add_days_field(self, "start", "Дата с")
        self.end = add_days_field(self, "end", "Дата по")
        self.text("minimum", "Минимальный F1", placeholder="Любое значение")
        self.button("Показать сводку", self.summary, footer=True)
        if level >= 2:
            self.button("Конструктор SELECT…", self.select)
            self.button("Поиск по шаблону…", self.search)
            self.button("Функции строк…", self.strings)
            self.button("Мастер JOIN…", self.join)
        if level >= 3:
            self.button("Подзапросы ANY / ALL / EXISTS…", self.subquery)
            self.button("Выражения CASE…", self.case)
            self.button("NULL: COALESCE и NULLIF…", self.null)
        if level >= 4:
            self.button("Конструктор CTE…", self.cte)
        if level >= 5:
            self.button("Оконные функции…", self.windows)

    @guarded
    def summary(self):
        start = None if self.start.date() == self.start.minimumDate() else self.start.date().toPython()
        end = None if self.end.date() == self.end.minimumDate() else self.end.date().toPython()
        if start and end and end < start:
            raise ValueError("Дата окончания должна быть не раньше начальной.")
        value = self.get("minimum").strip()
        headers, rows = self.db.summary(self.get("kind"), start, end, metric(value) if value else None)
        ResultDialog(self, "Сводка экспериментов", headers, rows).exec()

    @guarded
    def select(self):
        from .query_forms import SelectForm
        SelectForm(self, self.db, self.level).exec()

    @guarded
    def search(self):
        from .query_forms import search_form
        search_form(self, self.db, self.level)

    @guarded
    def strings(self):
        from .query_forms import string_form
        string_form(self, self.db)

    @guarded
    def join(self):
        from .query_forms import join_form
        join_form(self, self.db, self.level)

    @guarded
    def subquery(self):
        from .query_forms import subquery_form
        subquery_form(self, self.db)

    @guarded
    def case(self):
        from .query_forms import CaseForm
        CaseForm(self, self.db).exec()

    @guarded
    def null(self):
        from .query_forms import null_form
        null_form(self, self.db)

    @guarded
    def cte(self):
        from .report_forms import CteForm
        CteForm(self, self.db).exec()

    @guarded
    def windows(self):
        from .report_forms import WindowForm
        WindowForm(self, self.db).exec()


class MainWindow(QMainWindow):
    def __init__(self, level):
        super().__init__()
        self.level = level
        self.db = Database(level)
        self.setWindowTitle(f"Лабораторная №{level} · {LAB_TITLES[level - 1]}")
        self.resize(625, 570)
        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        settings = QGroupBox("Подключение к PostgreSQL")
        form = QFormLayout(settings)
        self.host = QLineEdit(os.environ.get("PGHOST", "localhost"))
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(5432)
        self.database = QLineEdit(os.environ.get("PGDATABASE", "postgres"))
        self.username = QLineEdit(os.environ.get("PGUSER", getpass.getuser()))
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Сервер", self.host)
        form.addRow("Порт", self.port)
        form.addRow("База данных", self.database)
        form.addRow("Пользователь", self.username)
        form.addRow("Пароль", self.password)
        connection_buttons = QHBoxLayout()
        self.add_button(connection_buttons, "Подключиться", self.connect_db)
        self.disconnect_button = self.add_button(connection_buttons, "Отключиться", self.disconnect_db)
        form.addRow(connection_buttons)
        layout.addWidget(settings)

        operations = QGroupBox(f"Эксперименты по ИИ-модерации · {self.db.schema}")
        actions = QVBoxLayout(operations)
        buttons = [
            ("Создать схему и таблицы", self.setup),
            ("Добавить демонстрационные данные", self.seed),
            ("Внести данные…", self.add_data),
            ("Показать данные…", self.show_data),
            ("Проверить внешний ключ и откат", self.foreign_key_demo),
        ]
        if level >= 2:
            buttons.append(("Изменить структуру (ALTER TABLE)…", self.alter))
        if level >= 3:
            buttons.append(("Пользовательские типы и таблицы…", self.types))
        if level >= 4:
            buttons.append(("Представления (VIEW)…", self.views))
        self.db_buttons = [self.add_button(actions, label, action) for label, action in buttons]
        layout.addWidget(operations)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.set_connected(False)

    @staticmethod
    def add_button(layout, label, callback):
        button = QPushButton(label)
        button.clicked.connect(lambda: callback())
        layout.addWidget(button)
        return button

    def set_connected(self, connected):
        for button in [*self.db_buttons, self.disconnect_button]:
            button.setEnabled(connected)
        self.status.setText("PostgreSQL подключён." if connected else "Подключитесь к PostgreSQL.")

    @guarded
    def connect_db(self):
        self.db.connect(host=require(self.host.text().strip(), "Укажите сервер."),
                        port=self.port.value(),
                        dbname=require(self.database.text().strip(), "Укажите базу данных."),
                        user=require(self.username.text().strip(), "Укажите пользователя."),
                        password=self.password.text() or None)
        self.password.clear()
        self.set_connected(True)

    def disconnect_db(self):
        self.db.close()
        self.set_connected(False)

    @guarded
    def setup(self):
        self.db.setup()
        self.status.setText(f"Схема {self.db.schema} и таблицы готовы.")

    @guarded
    def seed(self):
        self.db.seed()
        self.status.setText("Демонстрационные данные готовы.")

    @guarded
    def add_data(self):
        form = Form(self, "Внести данные", "Выберите, какую запись добавить.")
        form.combo("kind", "Тип записи", (("Датасет", "datasets"), ("Модель", "models"),
                                          ("Эксперимент", "experiments")))

        @guarded
        def open_insert(dialog):
            InsertDialog(dialog, self.db, dialog.get("kind")).exec()

        form.button("Открыть форму", lambda: open_insert(form), footer=True)
        form.exec()

    @guarded
    def show_data(self):
        SummaryDialog(self, self.db, self.level).exec()

    @guarded
    def foreign_key_demo(self):
        absent = sql.SQL("SELECT candidate FROM generate_series(-10, -1) AS candidates(candidate) "
                         "WHERE NOT EXISTS (SELECT 1 FROM {} WHERE id = candidate) LIMIT 1")
        ids = []
        for relation in ("datasets", "models"):
            rows = self.db.execute("SELECT свободный идентификатор для проверки",
                                   absent.format(self.db.relation(relation)))[1]
            if not rows:
                raise ValueError("Для примера нужен свободный отрицательный идентификатор.")
            ids.append(rows[0][0])
        try:
            with self.db.connection.transaction():
                self.db.insert("experiments", {
                    "title": "Проверка внешнего ключа", "dataset_id": ids[0], "model_id": ids[1],
                    "run_date": date.today(), "f1": Decimal("0.9"), "samples": 10,
                })
                raise ValueError("PostgreSQL принял неверную ссылку. Вставка отменена. Проверьте ограничения.")
        except psycopg.errors.ForeignKeyViolation as error:
            LOG.info("FOREIGN KEY %s: PostgreSQL отклонил запись; ROLLBACK", error.diag.constraint_name)
            QMessageBox.information(self, "Ссылочная целостность",
                                    "PostgreSQL отклонил ссылку на отсутствующую запись. "
                                    "Транзакция отменена (ROLLBACK).")

    @guarded
    def alter(self):
        from .structure_forms import AlterForm
        AlterForm(self, self.db).exec()

    @guarded
    def types(self):
        from .structure_forms import TypesForm
        TypesForm(self, self.db).exec()

    @guarded
    def views(self):
        from .report_forms import ViewsForm
        ViewsForm(self, self.db, self.level).exec()

    def closeEvent(self, event):
        self.db.close()
        super().closeEvent(event)


def main(level):
    configure_logging()
    app = QApplication(sys.argv)
    window = MainWindow(level)
    window.show()
    sys.exit(app.exec())
