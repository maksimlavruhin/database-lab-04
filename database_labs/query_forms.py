"""Формы SELECT, JOIN, подзапросов и вычисляемых выражений."""

from psycopg import sql

from . import queries
from .widgets import Form, guarded, set_options, show_query


def operators(level):
    return queries.COMPARISONS + queries.PATTERNS[:5 if level == 2 else 7]


def sources(db):
    return [name for name, _ in db.relations(("r", "p", "v", "m"))]


def table_form(parent, db, title, hint=""):
    form = Form(parent, title, hint)
    form.combo("table", "Источник", sources(db))
    return form


def run_form(form, db, title, build):
    @guarded
    def run(dialog):
        query, params = build()
        show_query(dialog, db, title, query, params)

    form.button("Выполнить", lambda: run(form), footer=True)
    form.exec()


class SelectForm(Form):
    def __init__(self, parent, db, level, save=None):
        super().__init__(parent, "Конструктор SELECT",
                         "Щёлкните по нужным столбцам. Пустой выбор означает все столбцы.")
        self.resize(715, 690)
        self.db = db
        self.group_sets = []
        table = self.combo("table", "Источник", sources(db))
        columns = self.column_list(db, "table", "columns", "Столбцы SELECT")
        self.filter_form(db, "table", operators=operators(level))
        self.filter_form(db, "table", "second", operators(level), "Второе поле WHERE (AND)")
        modes = ("GROUP BY", "ROLLUP", "CUBE", "GROUPING SETS") if level >= 4 else ("GROUP BY",)
        mode = self.combo("mode", "Способ группировки", modes)
        groups = self.column_list(db, "table", "groups", "Столбцы группировки")
        if level >= 4:
            self.sets_box = self.text_box("Наборы GROUPING SETS")
            self.add_set_button = self.button("Добавить выбранный набор", self.add_set)
            self.clear_sets_button = self.button("Очистить наборы", self.clear_sets)
        function = self.combo("function", "Агрегатная функция", queries.AGGREGATES, "Без агрегата")
        self.column_combo(db, "table", "aggregate", "Поле агрегата", "Все строки для COUNT(*)")
        self.combo("having", "Оператор HAVING", queries.COMPARISONS, "Без HAVING")
        self.text("having_value", "Значение HAVING")
        self.combo("order", "Поле ORDER BY")
        self.combo("direction", "Направление", ("ASC", "DESC"))
        self.button("Выполнить", self.run, footer=True)
        if save:
            self.button("Сохранить определение", lambda: self.use_definition(save), footer=True)

        table.currentIndexChanged.connect(self.clear_sets)
        columns.itemSelectionChanged.connect(self.sort_options)
        groups.itemSelectionChanged.connect(self.sort_options)
        mode.currentIndexChanged.connect(self.refresh_mode)
        function.currentIndexChanged.connect(lambda: self.sort_options())
        self.refresh_mode()

    @guarded
    def sort_options(self):
        mode = self.get("mode")
        if mode == "GROUPING SETS":
            fields = list(dict.fromkeys(field for group in self.group_sets for field in group))
        else:
            fields = self.get("groups")
        if not fields and mode != "GROUPING SETS" and not self.get("function"):
            fields = self.get("columns") or [field for field, in self.db.columns(self.get("table"))]
        if self.get("function"):
            fields = [*fields, "aggregate_value"]
        set_options(self.values["order"], fields, "Без сортировки")

    def refresh_mode(self):
        enabled = self.get("mode") == "GROUPING SETS"
        if hasattr(self, "sets_box"):
            for widget in (self.sets_box, self.add_set_button, self.clear_sets_button):
                self.form.setRowVisible(widget, enabled)
        self.sort_options()

    def clear_sets(self):
        self.group_sets.clear()
        self.show_sets()

    def add_set(self):
        fields = tuple(self.get("groups"))
        if fields not in self.group_sets:
            self.group_sets.append(fields)
        self.show_sets()

    def show_sets(self):
        if hasattr(self, "sets_box"):
            self.sets_box.setPlainText("\n".join(
                ", ".join(group) if group else "() — общий итог" for group in self.group_sets
            ))
        self.sort_options()

    def definition(self):
        table = self.required("table", "Создайте или выберите источник данных.")
        having = (self.get("having"), self.get("having_value")) if self.get("having") else None
        filters = [item for item in (self.filter_data(), self.filter_data("second")) if item]
        return queries.select_query(
            self.db, table, self.get("columns"), filters, self.get("groups"),
            self.get("mode"), self.group_sets, self.get("function"), self.get("aggregate"),
            having, self.get("order"), self.get("direction"),
        )

    @guarded
    def run(self):
        query, params = self.definition()
        show_query(self, self.db, "Результат SELECT", query, params)

    @guarded
    def use_definition(self, save):
        if save(*self.definition()):
            self.accept()


def search_form(parent, db, level):
    form = table_form(parent, db, "Поиск по шаблону",
                      "Для LIKE используйте % для любой последовательности и _ для одного символа.")
    form.column_combo(db, "table", "column", "Столбец")
    form.combo("op", "Способ поиска", queries.PATTERNS[:5 if level == 2 else 7])
    form.text("pattern", "Шаблон", placeholder="%Блог%")

    def build():
        table = form.required("table", "Выберите источник данных.")
        field = form.required("column", "Выберите столбец для поиска.")
        predicate, params = queries.condition(sql.Identifier(field), form.get("op"), form.get("pattern"))
        query = sql.SQL("SELECT * FROM {} WHERE {}").format(db.relation(table), predicate)
        return query, params

    run_form(form, db, "Поиск по шаблону", build)


def string_form(parent, db):
    form = table_form(parent, db, "Строковые функции")
    form.column_combo(db, "table", "column", "Исходный столбец")
    operation = form.combo("operation", "Функция", ("UPPER", "LOWER", "SUBSTRING", "TRIM", "LPAD", "RPAD", "CONCAT"))
    form.text("argument", "Позиция или текст")
    form.spin("count", "Длина", 5, 0)
    form.column_combo(db, "table", "second", "Второй столбец CONCAT", "Использовать текст")

    def set_fields():
        value = form.get("operation")
        form.hide_row("argument", value not in ("SUBSTRING", "LPAD", "RPAD", "CONCAT"))
        form.hide_row("count", value not in ("SUBSTRING", "LPAD", "RPAD"))
        form.hide_row("second", value != "CONCAT")

    operation.currentIndexChanged.connect(set_fields)
    set_fields()

    def build():
        return queries.string_query(
            db, form.required("table", "Выберите источник данных."),
            form.required("column", "Выберите исходный столбец."), form.get("operation"),
            form.get("argument"), form.get("count"), form.get("second"),
        )

    run_form(form, db, "Строковые функции", build)


def join_form(parent, db, level):
    form = Form(parent, "Мастер JOIN",
                "Выберите две таблицы и совместимые по типу поля. Для базовой схемы: experiments.dataset_id и datasets.id.")
    tables = sources(db)
    left = form.combo("left", "Левый источник", tables)
    right = form.combo("right", "Правый источник", tables)
    form.column_combo(db, "left", "left_key", "Ключ слева")
    form.column_combo(db, "right", "right_key", "Ключ справа")
    form.combo("kind", "Соединение", ("INNER", "LEFT", "RIGHT", "FULL"))
    filter_field = form.combo("filter", "Поле WHERE")
    form.combo("op", "Оператор", operators(level))
    form.text("value", "Значение")
    sort_field = form.combo("order", "Поле ORDER BY")
    form.combo("direction", "Направление", ("ASC", "DESC"))

    @guarded
    def options(dialog):
        fields = [(f"{label} · {field}", (alias, field))
                  for label, key, alias in (("Слева", "left", "l"), ("Справа", "right", "r"))
                  for field, in db.columns(dialog.get(key))]
        set_options(filter_field, fields, "Без условия")
        set_options(sort_field, fields, "Без сортировки")

    left.currentIndexChanged.connect(lambda: options(form))
    right.currentIndexChanged.connect(lambda: options(form))
    options(form)

    def build():
        filter_side, filter_key = form.get("filter") or ("", "")
        sort_side, sort_key = form.get("order") or ("", "")
        return queries.join_query(
            db, form.required("left", "Выберите левый источник."),
            form.required("right", "Выберите правый источник."),
            form.required("left_key", "Выберите левый ключ."),
            form.required("right_key", "Выберите правый ключ."),
            form.get("kind"), filter_side, filter_key, form.get("op"), form.get("value"),
            sort_side, sort_key, form.get("direction"),
        )

    run_form(form, db, "Результат JOIN", build)


def subquery_form(parent, db):
    form = Form(parent, "Подзапросы ANY / ALL / EXISTS",
                "Внутренний SELECT можно связать с текущей строкой внешнего запроса по совпадающим ключам.")
    form.resize(640, 590)
    tables = sources(db)
    form.combo("outer", "Внешний источник", tables)
    form.column_combo(db, "outer", "outer_field", "Поле для сравнения")
    form.combo("op", "Оператор", queries.COMPARISONS[:6])
    quantifier = form.combo("quantifier", "Условие", ("ANY", "ALL", "EXISTS"))
    form.combo("inner", "Источник подзапроса", tables)
    form.column_combo(db, "inner", "inner_field", "Поле подзапроса")
    form.filter_form(db, "inner", "inner", operators(3), "Фильтр подзапроса")
    correlated = form.check("correlated", "Коррелированный запрос")
    form.column_combo(db, "outer", "outer_key", "Ключ внешнего запроса")
    form.column_combo(db, "inner", "inner_key", "Ключ подзапроса")

    def set_fields():
        for key in ("outer_field", "op", "inner_field"):
            form.hide_row(key, form.get("quantifier") == "EXISTS")
        for key in ("outer_key", "inner_key"):
            form.hide_row(key, not form.get("correlated"))

    quantifier.currentIndexChanged.connect(set_fields)
    correlated.toggled.connect(set_fields)
    set_fields()

    def build():
        correlation = None
        if form.get("correlated"):
            correlation = (form.required("outer_key", "Выберите внешний ключ."),
                           form.required("inner_key", "Выберите внутренний ключ."))
        if form.get("quantifier") != "EXISTS":
            form.required("outer_field", "Выберите поле для сравнения.")
            form.required("inner_field", "Выберите поле подзапроса.")
        return queries.subquery(
            db, form.required("outer", "Выберите внешний источник."), form.get("outer_field"),
            form.get("op"), form.get("quantifier"), form.required("inner", "Выберите источник подзапроса."),
            form.get("inner_field"), form.filter_data("inner"), correlation,
        )

    run_form(form, db, "Результат подзапроса", build)


class CaseForm(Form):
    def __init__(self, parent, db):
        super().__init__(parent, "Конструктор CASE", "Правила проверяются сверху вниз. THEN и ELSE возвращают текст.")
        self.resize(640, 490)
        self.db = db
        self.rules = []
        table = self.combo("table", "Источник", sources(db))
        self.column_combo(db, "table", "column", "Поле WHEN")
        self.combo("operator", "Оператор WHEN", operators(3))
        self.text("value", "Значение WHEN")
        self.text("then", "Результат THEN")
        self.button("Добавить условие", self.add_rule)
        self.button("Очистить условия", self.clear_rules)
        self.rules_box = self.text_box("Порядок условий")
        self.text("otherwise", "Результат ELSE")
        self.button("Выполнить", self.run, footer=True)
        table.currentIndexChanged.connect(self.clear_rules)

    @guarded
    def add_rule(self):
        field = self.required("column", "Для WHEN выберите столбец.")
        op, value, result = self.get("operator"), self.get("value"), self.get("then")
        self.rules.append((field, op, value, result))
        shown = "" if op in ("IS NULL", "IS NOT NULL") else f" {value}"
        self.rules_box.appendPlainText(f"WHEN {field} {op}{shown} → {result}")

    def clear_rules(self):
        self.rules.clear()
        self.rules_box.clear()

    @guarded
    def run(self):
        table = self.required("table", "Выберите источник данных.")
        query, params = queries.case_query(self.db, table, self.rules, self.get("otherwise"))
        show_query(self, self.db, "Результат CASE", query, params)


def null_form(parent, db):
    form = table_form(parent, db, "Работа с NULL",
                      "COALESCE подставляет значение вместо NULL. NULLIF возвращает NULL при совпадении значений.")
    form.column_combo(db, "table", "column", "Столбец")
    form.combo("operation", "Функция", ("COALESCE", "NULLIF"))
    form.text("value", "Значение")

    def build():
        return queries.null_query(
            db, form.required("table", "Выберите источник данных."),
            form.required("column", "Выберите столбец."), form.get("operation"), form.get("value"),
        )

    run_form(form, db, "Результат обработки NULL", build)
