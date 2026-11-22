"""Подключение, транзакции и учебная схема PostgreSQL."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psycopg
from psycopg import ClientCursor, sql


PROJECT_DIR = Path(__file__).resolve().parent.parent
LOG = logging.getLogger("database_labs")
BUILTIN_TYPES = ("text", "integer", "numeric", "boolean", "date", "timestamp", "text[]")

ERRORS = {
    "23505": "Такая запись уже существует (UNIQUE).",
    "23502": "Обязательное поле осталось пустым (NOT NULL).",
    "23514": "Значение нарушает условие CHECK.",
    "23001": "Запись используется другой таблицей (RESTRICT).",
    "23503": "Связанная запись отсутствует или на неё уже ссылаются (FOREIGN KEY).",
    "22P02": "Значение не соответствует типу столбца.",
    "22001": "Текст длиннее допустимого размера.",
    "22003": "Число выходит за допустимый диапазон.",
    "22007": "Дата или время указаны в неверном формате.",
    "22008": "Дата или время выходят за допустимый диапазон.",
    "2201B": "Регулярное выражение составлено неверно.",
    "42804": "Выбранные типы данных несовместимы.",
    "42883": "Выбранный оператор или функция несовместимы с типом столбца.",
    "42703": "Столбец не найден. Проверьте структуру таблицы.",
    "42P01": "Объект не найден. Проверьте, создана ли схема и не переименованы ли таблицы.",
    "2BP01": "Объект используется другими объектами базы данных.",
    "42501": "У пользователя PostgreSQL недостаточно прав для этой операции.",
}


def configure_logging():
    if LOG.handlers:
        return
    handler = RotatingFileHandler(
        PROJECT_DIR / "database-labs.log", maxBytes=500_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def error_message(error):
    if isinstance(error, psycopg.Error):
        message = ERRORS.get(error.sqlstate)
        if message:
            name = error.diag.constraint_name
            return f"{message}\nОграничение: {name}" if name else message
        if isinstance(error, psycopg.OperationalError):
            return "Не удалось подключиться к PostgreSQL или соединение прервано. Проверьте сервер и параметры."
        return error.diag.message_primary or "PostgreSQL не выполнил операцию."
    return str(error)


def name(value):
    value = value.strip()
    if not value:
        raise ValueError("Укажите имя объекта.")
    if len(value.encode("utf-8")) > 63 or "\x00" in value:
        raise ValueError("Имя должно содержать до 63 байт и не включать нулевой символ.")
    return sql.Identifier(value)


class Database:
    def __init__(self, level):
        self.schema = f"moderation_lab{level}"
        self.connection = None

    def connect(self, **config):
        LOG.info("Попытка подключения к PostgreSQL")
        connection = psycopg.connect(**config, connect_timeout=5)
        self.close()
        self.connection = connection
        LOG.info("Подключение установлено")

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None
            LOG.info("Соединение закрыто")

    def relation(self, relation):
        return sql.Identifier(self.schema, relation)

    def execute(self, action, query, params=(), *, ddl=False):
        if self.connection is None or self.connection.closed:
            raise ValueError("Сначала подключитесь к PostgreSQL.")
        # DDL PostgreSQL требует литералы. ClientCursor подставляет их безопасно.
        with self.connection.transaction():
            cursor = ClientCursor(self.connection) if ddl else self.connection.cursor()
            with cursor:
                cursor.execute(query, params or None)
                if cursor.description:
                    result = [column.name for column in cursor.description], cursor.fetchall()
                else:
                    result = [], []
        LOG.info("%s", action)
        return result

    def setup(self):
        template = (Path(__file__).with_name("schema.sql")).read_text(encoding="utf-8")
        self.execute("CREATE SCHEMA, TYPE, TABLE", sql.SQL(template).format(
            schema=sql.Identifier(self.schema)
        ), ddl=True)

    def relations(self, kinds=("r", "p")):
        return self.execute("Список объектов схемы", """
            SELECT c.relname, c.relkind FROM pg_catalog.pg_class AS c
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relkind::text = ANY(%s)
            ORDER BY c.relname
        """, (self.schema, list(kinds)))[1]

    def columns(self, relation):
        if not relation:
            return []
        return self.execute("Столбцы объекта", """
            SELECT a.attname FROM pg_catalog.pg_attribute AS a
            JOIN pg_catalog.pg_class AS c ON c.oid = a.attrelid
            JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped
            ORDER BY a.attnum
        """, (self.schema, relation))[1]

    def types(self):
        return self.execute("Список пользовательских типов", """
            SELECT t.typname,
                   CASE t.typtype WHEN 'e' THEN 'ENUM' ELSE 'Составной' END,
                   CASE WHEN t.typtype = 'e' THEN (
                       SELECT string_agg(quote_literal(e.enumlabel), ', ' ORDER BY e.enumsortorder)
                       FROM pg_catalog.pg_enum AS e WHERE e.enumtypid = t.oid
                   ) ELSE (
                       SELECT string_agg(quote_ident(a.attname) || ' ' ||
                           pg_catalog.format_type(a.atttypid, a.atttypmod), ', ' ORDER BY a.attnum)
                       FROM pg_catalog.pg_attribute AS a
                       WHERE a.attrelid = t.typrelid AND a.attnum > 0 AND NOT a.attisdropped
                   ) END AS definition
            FROM pg_catalog.pg_type AS t
            JOIN pg_catalog.pg_namespace AS n ON n.oid = t.typnamespace
            LEFT JOIN pg_catalog.pg_class AS c ON c.oid = t.typrelid
            WHERE n.nspname = %s AND (t.typtype = 'e' OR c.relkind = 'c')
            ORDER BY t.typname
        """, (self.schema,))[1]

    def type_name(self, data_type):
        if data_type in BUILTIN_TYPES:
            return sql.SQL(data_type)
        return self.relation(data_type.removeprefix("Тип: "))

    def insert(self, table, values):
        query = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING id").format(
            self.relation(table),
            sql.SQL(", ").join(map(sql.Identifier, values)),
            sql.SQL(", ").join(sql.Placeholder() for _ in values),
        )
        return self.execute(f"INSERT {table}", query, tuple(values.values()))[1][0][0]

    def seed(self):
        schema = {
            key: self.relation(key) for key in ("datasets", "models", "experiments")
        }
        with self.connection.transaction():
            self.execute("INSERT демо: датасеты", sql.SQL("""
                INSERT INTO {datasets} (name, kind, labels, is_public) VALUES
                ('Отзывы магазина', 'reviews', ARRAY['обычный', 'спам'], true),
                ('Комментарии блога', 'comments', ARRAY['обычный', 'токсичный'], false),
                ('Сообщения поддержки', 'messages', ARRAY['обычный', 'оскорбление'], false)
                ON CONFLICT (name) DO NOTHING
            """).format(**schema))
            self.execute("INSERT демо: модели", sql.SQL("""
                INSERT INTO {models} (name, is_active) VALUES
                ('BERT Mini', true), ('TF-IDF Baseline', true), ('DistilBERT', false)
                ON CONFLICT (name) DO NOTHING
            """).format(**schema))
            self.execute("INSERT демо: эксперименты", sql.SQL("""
                INSERT INTO {experiments} (title, dataset_id, model_id, run_date, f1, samples, notes)
                SELECT demo.title, d.id, m.id, demo.run_date, demo.f1, demo.samples, demo.notes
                FROM (VALUES
                    ('Первый прогон', 'Отзывы магазина', 'TF-IDF Baseline',
                     DATE '2026-09-12', 0.7210, 500, NULL),
                    ('Очистка отзывов', 'Отзывы магазина', 'TF-IDF Baseline',
                     DATE '2026-09-20', 0.7840, 700, 'После очистки текста'),
                    ('Модель BERT', 'Отзывы магазина', 'BERT Mini',
                     DATE '2026-09-21', 0.8920, 800, NULL),
                    ('Блог: базовая', 'Комментарии блога', 'TF-IDF Baseline',
                     DATE '2026-09-16', 0.6830, 650, NULL),
                    ('Блог: сезонная выборка', 'Комментарии блога', 'BERT Mini',
                     DATE '2026-09-22', 0.8410, 900, 'Осенняя выборка'),
                    ('Блог: новый порог', 'Комментарии блога', 'BERT Mini',
                     DATE '2026-09-28', 0.8730, 950, 'Уточнён порог')
                ) AS demo(title, dataset, model, run_date, f1, samples, notes)
                JOIN {datasets} AS d ON d.name = demo.dataset
                JOIN {models} AS m ON m.name = demo.model
                ON CONFLICT (title, dataset_id, model_id, run_date) DO NOTHING
            """).format(**schema))

    def summary(self, kind="", date_from=None, date_to=None, minimum=None):
        conditions = []
        parameters = []
        for field, operator, value in (
            ("d.kind", "=", kind), ("e.run_date", ">=", date_from),
            ("e.run_date", "<=", date_to), ("e.f1", ">=", minimum),
        ):
            if value is not None and value != "":
                conditions.append(sql.SQL(f"{field} {operator} %s"))
                parameters.append(value)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(conditions) if conditions else sql.SQL("")
        query = sql.SQL("""
            SELECT e.id, e.title, d.name AS dataset, d.kind, m.name AS model,
                   e.run_date, e.f1, e.samples, e.notes
            FROM {} AS e JOIN {} AS d ON d.id = e.dataset_id
            JOIN {} AS m ON m.id = e.model_id
        """).format(*(self.relation(table) for table in ("experiments", "datasets", "models")))
        return self.execute("SELECT сводка экспериментов", query + where + sql.SQL(" ORDER BY e.id LIMIT 1001"), parameters)
