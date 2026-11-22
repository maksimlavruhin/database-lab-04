"""Представления, CTE и оконные функции."""

from psycopg import sql
from PySide6.QtWidgets import QMessageBox

from . import queries
from .db import name
from .query_forms import SelectForm, operators, sources
from .structure_forms import confirm
from .widgets import Form, guarded, select_items, set_options, show_query


class ViewsForm(Form):
    def __init__(self, parent, db, level):
        super().__init__(parent, "Представления",
                         "Для создания и изменения определения используйте конструктор SELECT.")
        self.db = db
        self.level = level
        self.items = self.combo("view", "Представление")
        self.button("Открыть структуру и SQL…", self.describe)
        self.button("Показать данные…", self.data)
        self.button("Создать VIEW или MATERIALIZED VIEW…", self.create)
        self.button("Заменить определение VIEW…", self.replace_view)
        self.button("Обновить данные MATERIALIZED VIEW", self.refresh_materialized)
        self.button("Удалить представление…", self.drop)
        self.refresh_list()

    def refresh_list(self):
        self.kinds = dict(self.db.relations(("v", "m")))
        set_options(self.items, [
            (f"{key} · {'VIEW' if kind == 'v' else 'MATERIALIZED VIEW'}", key)
            for key, kind in self.kinds.items()
        ])

    def selected_view(self):
        return self.required("view", "Выберите представление.")

    @guarded
    def create(self):
        form = Form(self, "Создать представление")
        form.text("name", "Имя")
        form.combo("kind", "Тип объекта", ("VIEW", "MATERIALIZED VIEW"))

        @guarded
        def configure(dialog):
            view = dialog.get("name").strip()
            name(view)
            kind = dialog.get("kind")
            prefix = sql.SQL("CREATE " + kind + " {} AS ").format(self.db.relation(view))
            if self.builder(dialog, f"CREATE {kind}", prefix):
                dialog.accept()

        form.button("Настроить выборку", lambda: configure(form), footer=True)
        if form.exec():
            self.refresh_list()

    def builder(self, parent, action, prefix):
        def save(query, params):
            self.db.execute(action, prefix + query, params, ddl=True)
            QMessageBox.information(parent, "Определение сохранено", "PostgreSQL сохранил представление.")
            return True

        return SelectForm(parent, self.db, self.level, save).exec()

    @guarded
    def describe(self):
        view = self.selected_view()
        ident = self.db.relation(view)
        definition = self.db.execute(
            "Просмотр определения VIEW",
            "SELECT pg_catalog.pg_get_viewdef(%s::regclass, true)",
            (ident.as_string(self.db.connection),),
        )[1][0][0]
        form = Form(self, f"Структура · {view}")
        form.resize(740, 420)
        form.list("columns", "Поля", [column for column, in self.db.columns(view)])
        editor = form.text_box("Определение", definition or "")
        editor.setMaximumHeight(260)
        form.exec()

    @guarded
    def data(self):
        view = self.selected_view()
        query = sql.SQL("SELECT * FROM {}").format(self.db.relation(view))
        show_query(self, self.db, f"Данные · {view}", query)

    @guarded
    def replace_view(self):
        view = self.selected_view()
        if self.kinds[view] != "v":
            raise ValueError("Выберите обычное представление VIEW.")
        prefix = sql.SQL("CREATE OR REPLACE VIEW {} AS ").format(self.db.relation(view))
        self.builder(self, "CREATE OR REPLACE VIEW", prefix)

    @guarded
    def refresh_materialized(self):
        view = self.selected_view()
        if self.kinds[view] != "m":
            raise ValueError("Выберите MATERIALIZED VIEW.")
        query = sql.SQL("REFRESH MATERIALIZED VIEW {}").format(self.db.relation(view))
        self.db.execute("REFRESH MATERIALIZED VIEW", query, ddl=True)
        QMessageBox.information(self, "Данные обновлены", "Запрос представления выполнен повторно.")

    @guarded
    def drop(self):
        view = self.selected_view()
        if confirm(self, f"Удалить представление {view}?"):
            kind = "VIEW" if self.kinds[view] == "v" else "MATERIALIZED VIEW"
            self.db.execute(f"DROP {kind}", sql.SQL("DROP " + kind + " {}").format(
                self.db.relation(view)
            ), ddl=True)
            self.refresh_list()


class CteForm(Form):
    def __init__(self, parent, db):
        super().__init__(parent, "Конструктор CTE",
                         "Добавляйте блоки сверху вниз. Источником следующего блока может быть предыдущий CTE.")
        self.resize(700, 620)
        self.db = db
        self.blocks = []
        self.block_columns = {}
        self.sources = sources(db)
        self.text("name", "Имя блока")
        source = self.combo("source", "Источник блока")
        self.columns = self.list("columns", "Столбцы (пусто — все)")
        self.filter = self.combo("filter", "Поле WHERE")
        self.combo("op", "Оператор", operators(3))
        self.text("value", "Значение")
        self.button("Добавить блок", self.add_block)
        self.button("Очистить блоки", self.clear_blocks)
        self.preview = self.text_box("Блоки WITH")
        self.output = self.combo("output", "Какой блок вывести")
        self.button("Выполнить", self.run, footer=True)
        source.currentIndexChanged.connect(lambda: self.source_changed())
        self.refresh_sources()

    def refresh_sources(self):
        ctes = [(f"CTE · {key}", (key, True)) for key in self.block_columns]
        tables = [(f"БД · {key}", (key, False)) for key in self.sources]
        set_options(self.values["source"], [*tables, *ctes])
        set_options(self.output, list(self.block_columns))
        if self.output.count():
            self.output.setCurrentIndex(self.output.count() - 1)
        self.source_changed()

    @guarded
    def source_changed(self):
        source, from_cte = self.get("source") or ("", False)
        if from_cte:
            fields = self.block_columns[source]
        else:
            fields = [field for field, in self.db.columns(source)]
        self.available_fields = fields
        select_items(self.columns, fields)
        set_options(self.filter, fields, "Без условия")

    @guarded
    def add_block(self):
        block_name = self.get("name").strip()
        name(block_name)
        if block_name in self.block_columns:
            raise ValueError("Блок с таким именем уже добавлен.")
        source, from_cte = self.required("source", "Выберите источник блока.")
        columns = self.get("columns")
        predicate = self.filter_data()
        self.blocks.append((block_name, source, from_cte, columns, predicate))
        self.block_columns[block_name] = columns or self.available_fields
        note = f" · WHERE {predicate[0]} {predicate[1]}" if predicate else ""
        self.preview.appendPlainText(f"{block_name} ← {source} · {', '.join(columns) or '*'}{note}")
        self.values["name"].clear()
        self.refresh_sources()

    def clear_blocks(self):
        self.blocks.clear()
        self.block_columns.clear()
        self.preview.clear()
        self.refresh_sources()

    @guarded
    def run(self):
        output = self.required("output", "Добавьте блок CTE и выберите его для вывода.")
        query, params = queries.cte_query(self.db, self.blocks, output)
        show_query(self, self.db, "Результат WITH", query, params)


class WindowForm(Form):
    def __init__(self, parent, db):
        super().__init__(parent, "Оконные функции",
                         "Добавьте поля сортировки по очереди. Разбиение задаёт группы, внутри которых считается функция.")
        self.resize(670, 610)
        self.db = db
        self.order = []
        table = self.combo("table", "Источник", sources(db))
        function = self.combo("function", "Функция", ("RANK", "LAG", "LEAD"))
        self.column_combo(db, "table", "column", "Столбец для LAG / LEAD")
        self.spin("offset", "Смещение", 1, 0, 10_000)
        self.column_list(db, "table", "partition", "Поля PARTITION BY")
        self.column_combo(db, "table", "order_field", "Поле ORDER BY")
        self.combo("direction", "Направление", ("ASC", "DESC"))
        self.button("Добавить ключ сортировки", self.add_order)
        self.button("Очистить порядок", self.clear_order)
        self.preview = self.text_box("Порядок ключей")
        self.button("Выполнить", self.run, footer=True)
        function.currentIndexChanged.connect(self.set_fields)
        table.currentIndexChanged.connect(self.clear_order)
        self.set_fields()

    def set_fields(self):
        rank = self.get("function") == "RANK"
        self.hide_row("column", rank)
        self.hide_row("offset", rank)

    @guarded
    def add_order(self):
        field = self.required("order_field", "Выберите поле сортировки.")
        if any(key == field for key, _ in self.order):
            raise ValueError("Этот столбец уже добавлен в порядок сортировки.")
        direction = self.get("direction")
        self.order.append((field, direction))
        self.preview.appendPlainText(f"{field} {direction}")

    def clear_order(self):
        self.order.clear()
        self.preview.clear()

    @guarded
    def run(self):
        table = self.required("table", "Выберите источник данных.")
        if self.get("function") != "RANK":
            self.required("column", "Выберите столбец для сравнения.")
        query, params = queries.window_query(
            self.db, table, self.get("function"), self.get("column"),
            self.get("partition"), self.order, self.get("offset"),
        )
        show_query(self, self.db, "Результат оконной функции", query, params)
