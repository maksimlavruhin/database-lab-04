"""Изменение структуры таблиц и пользовательских типов."""

from psycopg import sql
from PySide6.QtWidgets import QMessageBox

from . import queries
from .db import BUILTIN_TYPES, name
from .widgets import Form, ResultDialog, guarded, set_options


def confirm(parent, question):
    return QMessageBox.question(parent, "Подтвердите изменение", question) == QMessageBox.StandardButton.Yes


def type_choices(db):
    return (*BUILTIN_TYPES, *(f"Тип: {type_name}" for type_name, _, _ in db.types()))


ALTER_FIELDS = {
    "Добавить столбец": ("new", "type"),
    "Удалить столбец": ("column",),
    "Переименовать таблицу": ("new",),
    "Переименовать столбец": ("column", "new"),
    "Изменить тип столбца": ("column", "type"),
    "Добавить NOT NULL": ("column",),
    "Убрать NOT NULL": ("column",),
    "Добавить CHECK": ("column", "constraint_name", "op", "value"),
    "Добавить UNIQUE": ("column", "constraint_name"),
    "Добавить FOREIGN KEY": ("column", "constraint_name", "reference", "reference_key", "delete", "update"),
    "Убрать CHECK / UNIQUE / FOREIGN KEY": ("constraint",),
}


class AlterForm(Form):
    def __init__(self, parent, db):
        super().__init__(parent, "Изменить структуру · ALTER TABLE",
                         "Изменения затрагивают выбранную схему. При ошибке PostgreSQL отменяет операцию.")
        self.resize(665, 480)
        self.db = db
        self.tables = self.combo("table", "Таблица")
        action = self.combo("action", "Операция", list(ALTER_FIELDS))
        self.column_combo(db, "table", "column", "Столбец")
        self.text("new", "Новое имя")
        self.combo("type", "Тип данных", type_choices(db))
        self.text("constraint_name", "Имя ограничения")
        self.combo("op", "Условие CHECK", queries.COMPARISONS)
        self.text("value", "Значение CHECK")
        self.combo("reference", "Таблица для ссылки")
        self.column_combo(db, "reference", "reference_key", "Столбец для ссылки")
        self.combo("delete", "ON DELETE", ("RESTRICT", "NO ACTION", "CASCADE", "SET NULL"))
        self.combo("update", "ON UPDATE", ("CASCADE", "RESTRICT", "NO ACTION", "SET NULL"))
        self.combo("constraint", "Существующее ограничение")
        self.button("Применить", self.apply, footer=True)
        self.tables.currentIndexChanged.connect(lambda: self.refresh_constraints())
        action.currentIndexChanged.connect(self.refresh_operation)
        self.refresh_tables()
        self.refresh_operation()

    def refresh_tables(self, focus=""):
        tables = [table for table, _ in self.db.relations()]
        set_options(self.tables, tables)
        set_options(self.values["reference"], tables)
        if focus in tables:
            self.tables.setCurrentIndex(self.tables.findData(focus))
        self.refresh_columns(self.db, "table")
        self.refresh_columns(self.db, "reference")
        self.refresh_constraints()

    @guarded
    def refresh_constraints(self):
        rows = self.db.execute("Список ограничений", """
            SELECT co.conname, co.contype::text FROM pg_catalog.pg_constraint AS co
            JOIN pg_catalog.pg_class AS c ON c.oid = co.conrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s AND co.contype IN ('c', 'u', 'f')
            ORDER BY co.conname
        """, (self.db.schema, self.get("table")))[1]
        labels = {"c": "CHECK", "u": "UNIQUE", "f": "FOREIGN KEY"}
        set_options(self.values["constraint"], [(f"{key} · {labels[kind]}", key) for key, kind in rows])

    def refresh_operation(self):
        enabled = ALTER_FIELDS[self.get("action")]
        for key in self.values:
            if key not in ("table", "action"):
                self.hide_row(key, key not in enabled)

    @guarded
    def apply(self):
        table = self.required("table", "Выберите таблицу.")
        action = self.get("action")
        command, params, focus = self.build(action)
        if action.startswith(("Удалить", "Убрать")):
            if not confirm(self, f"Применить «{action}» к таблице {table}?"):
                return
        query = sql.SQL("ALTER TABLE {} ").format(self.db.relation(table)) + command
        self.db.execute(f"ALTER TABLE {table}: {action}", query, params, ddl=True)
        self.refresh_tables(focus)
        QMessageBox.information(self, "Структура изменена", "PostgreSQL сохранил изменение.")

    def build(self, action):
        params, focus = [], ""
        fields = ALTER_FIELDS[action]
        column = name(self.get("column")) if "column" in fields else None
        new = name(self.get("new")) if "new" in fields else None
        data_type = self.db.type_name(self.get("type")) if "type" in fields else None

        if action == "Добавить столбец":
            command = sql.SQL("ADD COLUMN {} {}").format(new, data_type)
        elif action == "Удалить столбец":
            command = sql.SQL("DROP COLUMN {}").format(column)
        elif action == "Переименовать таблицу":
            command = sql.SQL("RENAME TO {}").format(new)
            focus = self.get("new").strip()
        elif action == "Переименовать столбец":
            command = sql.SQL("RENAME COLUMN {} TO {}").format(column, new)
        elif action == "Изменить тип столбца":
            command = sql.SQL("ALTER COLUMN {} TYPE {} USING {}::{}").format(column, data_type, column, data_type)
        elif action == "Добавить NOT NULL":
            command = sql.SQL("ALTER COLUMN {} SET NOT NULL").format(column)
        elif action == "Убрать NOT NULL":
            command = sql.SQL("ALTER COLUMN {} DROP NOT NULL").format(column)
        elif action == "Убрать CHECK / UNIQUE / FOREIGN KEY":
            command = sql.SQL("DROP CONSTRAINT {}").format(name(self.get("constraint")))
        else:
            prefix = sql.SQL("ADD CONSTRAINT {} ").format(name(self.get("constraint_name")))
            if action == "Добавить CHECK":
                expression, params = queries.condition(column, self.get("op"), self.get("value"))
                command = prefix + sql.SQL("CHECK ({})").format(expression)
            elif action == "Добавить UNIQUE":
                command = prefix + sql.SQL("UNIQUE ({})").format(column)
            else:
                delete, update = self.get("delete"), self.get("update")
                command = prefix + sql.SQL(
                    "FOREIGN KEY ({}) REFERENCES {} ({}) ON DELETE {} ON UPDATE {}"
                ).format(
                    column, self.db.relation(self.required("reference", "Выберите таблицу для ссылки.")),
                    name(self.get("reference_key")), sql.SQL(delete), sql.SQL(update),
                )
        return command, params, focus


class ColumnsForm(Form):
    def __init__(self, parent, db, composite):
        title = "Новый составной тип" if composite else "Новая таблица"
        hint = "Добавляйте атрибуты по одному." if composite else "Столбец id создаётся автоматически как первичный ключ."
        super().__init__(parent, title, hint)
        self.resize(600, 410)
        self.db = db
        self.composite = composite
        self.columns = {}
        self.text("name", "Имя объекта")
        self.text("column", "Имя атрибута" if composite else "Имя столбца")
        self.combo("type", "Тип данных", type_choices(db))
        self.button("Добавить в список", self.add_column)
        self.button("Очистить список", self.clear_columns)
        self.preview = self.text_box("Поля")
        self.button("Создать", self.save, footer=True)

    @guarded
    def add_column(self):
        column = self.get("column").strip()
        name(column)
        if column in self.columns or (column == "id" and not self.composite):
            raise ValueError("Поле с таким именем уже есть.")
        data_type = self.get("type")
        self.columns[column] = data_type
        self.preview.appendPlainText(f"{column} · {data_type}")
        self.values["column"].clear()

    def clear_columns(self):
        self.columns.clear()
        self.preview.clear()

    @guarded
    def save(self):
        name(self.get("name"))
        target = self.db.relation(self.get("name").strip())
        queries.require(self.columns, "Добавьте хотя бы одно поле в список.")
        fields = [sql.SQL("{} {}").format(name(field), self.db.type_name(data_type))
                  for field, data_type in self.columns.items()]
        if self.composite:
            statement = "CREATE TYPE {} AS ({})"
            action = "CREATE TYPE (составной)"
        else:
            fields.insert(0, sql.SQL("id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY"))
            statement = "CREATE TABLE {} ({})"
            action = "CREATE TABLE (пользовательская)"
        self.db.execute(action, sql.SQL(statement).format(target, queries.join(fields)), ddl=True)
        self.accept()


class TypesForm(Form):
    def __init__(self, parent, db):
        super().__init__(parent, "Пользовательские типы и таблицы")
        self.db = db
        self.items = self.combo("type", "Пользовательский тип")
        self.details = self.text_box("Определение")
        self.button("Создать ENUM…", self.create_enum)
        self.button("Создать составной тип…", self.create_composite)
        self.button("Создать таблицу с выбранными типами…", self.create_table)
        self.button("Переименовать тип…", self.rename)
        self.button("Добавить метку ENUM…", self.add_label)
        self.button("Удалить тип…", self.drop)
        self.button("Показать все типы", self.show_all, footer=True)
        self.items.currentIndexChanged.connect(self.describe)
        self.refresh()

    def refresh(self):
        self.records = {key: (kind, description) for key, kind, description in self.db.types()}
        set_options(self.items, [(f"{key} · {kind}", key) for key, (kind, _) in self.records.items()])
        self.describe()

    def describe(self):
        record = self.records.get(self.get("type"))
        self.details.setPlainText(record[1] or "" if record else "")

    @guarded
    def show_all(self):
        ResultDialog(self, "Типы текущей схемы", ("Имя", "Вид", "Определение"), self.db.types()).exec()

    @guarded
    def create_enum(self):
        form = Form(self, "Новый ENUM", "Введите разные значения, разделённые запятыми.")
        form.text("name", "Имя типа")
        form.text("labels", "Метки", placeholder="draft, approved, archived")

        @guarded
        def save(dialog):
            type_name = dialog.get("name").strip()
            name(type_name)
            labels = [label.strip() for label in dialog.get("labels").split(",") if label.strip()]
            if not labels or len(labels) != len(set(labels)):
                raise ValueError("Укажите одну или несколько разных меток.")
            self.db.execute("CREATE TYPE ENUM", sql.SQL("CREATE TYPE {} AS ENUM ({})").format(
                self.db.relation(type_name), queries.join(map(sql.Literal, labels))
            ), ddl=True)
            dialog.accept()

        form.button("Создать", lambda: save(form), footer=True)
        if form.exec():
            self.refresh()

    @guarded
    def create_composite(self):
        if ColumnsForm(self, self.db, composite=True).exec():
            self.refresh()

    @guarded
    def create_table(self):
        ColumnsForm(self, self.db, composite=False).exec()

    def edit_type(self, title, label, template, as_identifier):
        selected = self.required("type", "Выберите пользовательский тип.")
        form = Form(self, title)
        form.text("value", label)

        @guarded
        def save(dialog):
            value = dialog.get("value").strip()
            queries.require(value, "Введите значение.")
            expression = name(value) if as_identifier else sql.Literal(value)
            self.db.execute(title, sql.SQL(template).format(self.db.relation(selected), expression), ddl=True)
            dialog.accept()

        form.button("Применить", lambda: save(form), footer=True)
        if form.exec():
            self.refresh()

    @guarded
    def rename(self):
        self.edit_type("Переименовать тип", "Новое имя", "ALTER TYPE {} RENAME TO {}", True)

    @guarded
    def add_label(self):
        selected = self.required("type", "Выберите ENUM.")
        if self.records[selected][0] != "ENUM":
            raise ValueError("Добавлять метки можно к типу ENUM.")
        self.edit_type("Добавить метку ENUM", "Новая метка", "ALTER TYPE {} ADD VALUE {}", False)

    @guarded
    def drop(self):
        selected = self.required("type", "Выберите пользовательский тип.")
        if confirm(self, f"Удалить тип {selected}?"):
            self.db.execute("DROP TYPE", sql.SQL("DROP TYPE {}").format(self.db.relation(selected)), ddl=True)
            self.refresh()
